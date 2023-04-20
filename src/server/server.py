'''RotorHazard server script'''
RELEASE_VERSION = "4.0.0-dev.3" # Public release version code
SERVER_API = 39 # Server API version
NODE_API_SUPPORTED = 18 # Minimum supported node version
NODE_API_BEST = 35 # Most recent node API
JSON_API = 3 # JSON API version

# This must be the first import for the time being. It is
# necessary to set up logging *before* anything else
# because there is a lot of code run through imports, and
# we would miss messages otherwise.
import logging
import log
from datetime import datetime
from monotonic import monotonic
import RHTimeFns

log.early_stage_setup()
logger = logging.getLogger(__name__)

EPOCH_START = RHTimeFns.getEpochStartTime()

# program-start time, in milliseconds since 1970-01-01
PROGRAM_START_EPOCH_TIME = int((RHTimeFns.getUtcDateTimeNow() - EPOCH_START).total_seconds() * 1000)

# program-start time (in milliseconds, starting at zero)
PROGRAM_START_MTONIC = monotonic()

# offset for converting 'monotonic' time to epoch milliseconds since 1970-01-01
MTONIC_TO_EPOCH_MILLIS_OFFSET = PROGRAM_START_EPOCH_TIME - 1000.0*PROGRAM_START_MTONIC

logger.info('RotorHazard v{0}'.format(RELEASE_VERSION))

# Normal importing resumes here
import gevent.monkey
gevent.monkey.patch_all()

import io
import os
import sys
import base64
import subprocess
import importlib
# import copy
from functools import wraps
from six import string_types

from flask import Flask, send_file, request, Response, session, templating, redirect, abort, copy_current_request_context
from flask_socketio import SocketIO, emit

import socket
import random
import string
import json

import Config
import Database
import Results
import Language
import json_endpoints
import EventActions
import RHData
import RHUtils
from RHUtils import catchLogExceptionsWrapper
import RHUI
from ClusterNodeSet import SecondaryNode, ClusterNodeSet
import PageCache
from util.InvokeFuncQueue import InvokeFuncQueue
import RHGPIO
from util.ButtonInputHandler import ButtonInputHandler
import util.stm32loader as stm32loader

# Events manager
from eventmanager import Evt, EventManager

Events = EventManager()
EventActionsObj = None

# LED imports
from led_event_manager import LEDEventManager, NoLEDManager, ClusterLEDManager, LEDEvent, Color, ColorVal, ColorPattern, hexToColor

sys.path.append('../interface')
sys.path.append('/home/pi/RotorHazard/src/interface')  # Needed to run on startup

from Plugins import search_modules  #pylint: disable=import-error
from Sensors import Sensors  #pylint: disable=import-error
import RHRace
from RHRace import StartBehavior, WinCondition, WinStatus, RaceStatus, StagingTones
from data_export import DataExportManager
from VRxControl import VRxControlManager
from HeatGenerator import HeatGeneratorManager

APP = Flask(__name__, static_url_path='/static')

HEARTBEAT_THREAD = None
BACKGROUND_THREADS_ENABLED = True
HEARTBEAT_DATA_RATE_FACTOR = 5

ERROR_REPORT_INTERVAL_SECS = 600  # delay between comm-error reports to log

DB_FILE_NAME = 'database.db'
DB_BKP_DIR_NAME = 'db_bkp'
IMDTABLER_JAR_NAME = 'static/IMDTabler.jar'
NODE_FW_PATHNAME = "firmware/RH_S32_BPill_node.bin"

# check if 'log' directory owned by 'root' and change owner to 'pi' user if so
if RHUtils.checkSetFileOwnerPi(log.LOG_DIR_NAME):
    logger.info("Changed '{0}' dir owner from 'root' to 'pi'".format(log.LOG_DIR_NAME))

# command-line arguments:
CMDARG_VERSION_LONG_STR = '--version'    # show program version and exit
CMDARG_VERSION_SHORT_STR = '-v'          # show program version and exit
CMDARG_ZIP_LOGS_STR = '--ziplogs'        # create logs .zip file
CMDARG_JUMP_TO_BL_STR = '--jumptobl'     # send jump-to-bootloader command to node
CMDARG_FLASH_BPILL_STR = '--flashbpill'  # flash firmware onto S32_BPill processor
CMDARG_VIEW_DB_STR = '--viewdb'          # load and view given database file
CMDARG_LAUNCH_B_STR = '--launchb'        # launch browser on local computer

if __name__ == '__main__' and len(sys.argv) > 1:
    if CMDARG_VERSION_LONG_STR in sys.argv or CMDARG_VERSION_SHORT_STR in sys.argv:
        sys.exit(0)
    if CMDARG_ZIP_LOGS_STR in sys.argv:
        log.create_log_files_zip(logger, Config.CONFIG_FILE_NAME, DB_FILE_NAME)
        sys.exit(0)
    if CMDARG_VIEW_DB_STR in sys.argv:
        viewdbArgIdx = sys.argv.index(CMDARG_VIEW_DB_STR) + 1
        if viewdbArgIdx < len(sys.argv):
            if not os.path.exists(sys.argv[viewdbArgIdx]):
                print("Unable to find given DB file: {0}".format(sys.argv[viewdbArgIdx]))
                sys.exit(1)
        else:
            print("Usage: python server.py {0} dbFileName.db [pagename] [browsercmd]".format(CMDARG_VIEW_DB_STR))
            sys.exit(1)
    elif CMDARG_JUMP_TO_BL_STR not in sys.argv:  # handle jump-to-bootloader argument later
        if CMDARG_FLASH_BPILL_STR in sys.argv:
            flashPillArgIdx = sys.argv.index(CMDARG_FLASH_BPILL_STR) + 1
            flashPillPortStr = Config.SERIAL_PORTS[0] if Config.SERIAL_PORTS and \
                                                len(Config.SERIAL_PORTS) > 0 else None
            flashPillSrcStr = sys.argv[flashPillArgIdx] if flashPillArgIdx < len(sys.argv) else None
            if flashPillSrcStr and flashPillSrcStr.startswith("--"):  # use next arg as src file (optional)
                flashPillSrcStr = None                       #  unless arg is switch param
            flashPillSuccessFlag = stm32loader.flash_file_to_stm32(flashPillPortStr, flashPillSrcStr)
            sys.exit(0 if flashPillSuccessFlag else 1)
        elif CMDARG_LAUNCH_B_STR not in sys.argv:
            print("Unrecognized command-line argument(s): {0}".format(sys.argv[1:]))
            sys.exit(1)

BASEDIR = os.getcwd()
APP.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASEDIR, DB_FILE_NAME)
APP.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
Database.DB.init_app(APP)
Database.DB.app = APP

# start SocketIO service
SOCKET_IO = SocketIO(APP, async_mode='gevent', cors_allowed_origins=Config.GENERAL['CORS_ALLOWED_HOSTS'])

# this is the moment where we can forward log-messages to the frontend, and
# thus set up logging for good.
Current_log_path_name = log.later_stage_setup(Config.LOGGING, SOCKET_IO)

INTERFACE = None  # initialized later
SENSORS = Sensors()
CLUSTER = None    # initialized later
ClusterSendAckQueueObj = None
PassInvokeFuncQueueObj = InvokeFuncQueue(logger)
serverInfo = None
serverInfoItems = None
Use_imdtabler_jar_flag = False  # set True if IMDTabler.jar is available
server_ipaddress_str = None
ShutdownButtonInputHandler = None
Server_secondary_mode = None

RACE = RHRace.RHRace() # For storing race management variables
LAST_RACE = None
SECONDARY_RACE_FORMAT = None
RHData = RHData.RHData(Database, Events, RACE, SERVER_API, DB_FILE_NAME, DB_BKP_DIR_NAME) # Primary race data storage
RACE._rhdata = RHData

PageCache = PageCache.PageCache(RHData, Events) # For storing page cache
Language = Language.Language(RHData) # initialize language
__ = Language.__ # Shortcut to translation function
Database.__ = __ # Pass language to Database module

led_manager = NoLEDManager()
vrx_manager = None

RHUI = RHUI.RHUI(APP, SOCKET_IO, Events, RACE, LAST_RACE, SENSORS, CLUSTER, RHData, Language, PageCache, led_manager, vrx_manager) # User Interface Manager
RHUI.__ = Language.__ # Pass translation shortcut

RHData.late_init(PageCache, Language) # Give RHData additional references

ui_server_messages = {}
def set_ui_message(mainclass, message, header=None, subclass=None):
    item = {}
    item['message'] = message
    if header:
        item['header'] = __(header)
    if subclass:
        item['subclass'] = subclass
    ui_server_messages[mainclass] = item

# convert 'monotonic' time to epoch milliseconds since 1970-01-01
def monotonic_to_epoch_millis(secs):
    return 1000.0*secs + MTONIC_TO_EPOCH_MILLIS_OFFSET

# Wrapper to be used as a decorator on callback functions that do database calls,
#  so their exception details are sent to the log file (instead of 'stderr')
#  and the database session is closed on thread exit (prevents DB-file handles left open).
def catchLogExcDBCloseWrapper(func):
    def wrapper(*args, **kwargs):
        try:
            retVal = func(*args, **kwargs)
            RHData.close()
            return retVal
        except:
            logger.exception("Exception via catchLogExcDBCloseWrapper")
            try:
                RHData.close()
            except:
                logger.exception("Error closing DB session in catchLogExcDBCloseWrapper-catch")
    return wrapper

# Return 'DEF_NODE_FWUPDATE_URL' config value; if not set in 'config.json'
#  then return default value based on BASEDIR and server RELEASE_VERSION
def getDefNodeFwUpdateUrl():
    try:
        if Config.GENERAL['DEF_NODE_FWUPDATE_URL']:
            return Config.GENERAL['DEF_NODE_FWUPDATE_URL']
        if RELEASE_VERSION.lower().find("dev") > 0:  # if "dev" server version then
            retStr = stm32loader.DEF_BINSRC_STR      # use current "dev" firmware at URL
        else:
            # return path that is up two levels from BASEDIR, and then NODE_FW_PATHNAME
            retStr = os.path.abspath(os.path.join(os.path.join(os.path.join(BASEDIR, os.pardir), \
                                                             os.pardir), NODE_FW_PATHNAME))
        # check if file with better-matching processor type (i.e., STM32F4) is available
        try:
            curTypStr = INTERFACE.nodes[0].firmware_proctype_str if len(INTERFACE.nodes) else None
            if curTypStr:
                fwTypStr = getFwfileProctypeStr(retStr)
                if fwTypStr and curTypStr != fwTypStr:
                    altFwFNameStr = RHUtils.appendToBaseFilename(retStr, ('_'+curTypStr))
                    altFwTypeStr = getFwfileProctypeStr(altFwFNameStr)
                    if curTypStr == altFwTypeStr:
                        logger.debug("Using better-matching node-firmware file: " + altFwFNameStr)
                        return altFwFNameStr
        except Exception as ex:
            logger.debug("Error checking fw type vs current type: " + str(ex))
        return retStr
    except:
        logger.exception("Error determining value for 'DEF_NODE_FWUPDATE_URL'")
    return "/home/pi/RotorHazard/" + NODE_FW_PATHNAME

# Returns the processor-type string from the given firmware file, or None if not found
def getFwfileProctypeStr(fileStr):
    dataStr = None
    try:
        dataStr = stm32loader.load_source_file(fileStr, False)
        if dataStr:
            return RHUtils.findPrefixedSubstring(dataStr, INTERFACE.FW_PROCTYPE_PREFIXSTR, \
                                                 INTERFACE.FW_TEXT_BLOCK_SIZE)
    except Exception as ex:
        logger.debug("Error processing file '{}' in 'getFwfileProctypeStr()': {}".format(fileStr, ex))
    return None


#
# Authentication
#

def check_auth(username, password):
    '''Check if a username password combination is valid.'''
    return username == Config.GENERAL.get('ADMIN_USERNAME') and password == Config.GENERAL.get('ADMIN_PASSWORD')

def authenticate():
    '''Sends a 401 response that enables basic auth.'''
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    if Config.GENERAL.get('ADMIN_USERNAME') != "" or \
                            Config.GENERAL.get('ADMIN_PASSWORD') != "":
        @wraps(f)
        def decorated_auth(*args, **kwargs):
            auth = request.authorization
            if not auth or not check_auth(auth.username, auth.password):
                return authenticate()
            return f(*args, **kwargs)
        return decorated_auth
    # allow open access if both ADMIN fields set to empty string:
    @wraps(f)
    def decorated_noauth(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated_noauth

# Flask template render with exception catch, so exception
# details are sent to the log file (instead of 'stderr').
def render_template(template_name_or_list, **context):
    try:
        return templating.render_template(template_name_or_list, **context)
    except Exception:
        logger.exception("Exception in render_template")
    return "Error rendering template"

#
# Routes
#

@APP.route('/')
def render_index():
    '''Route to home page.'''
    return render_template('home.html', serverInfo=serverInfo,
                           getOption=RHData.get_option, __=__, Debug=Config.GENERAL['DEBUG'])

@APP.route('/event')
def render_event():
    '''Route to heat summary page.'''
    return render_template('event.html', num_nodes=RACE.num_nodes, serverInfo=serverInfo, getOption=RHData.get_option, __=__)

@APP.route('/results')
def render_results():
    '''Route to round summary page.'''
    return render_template('results.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__, Debug=Config.GENERAL['DEBUG'])

@APP.route('/run')
@requires_auth
def render_run():
    '''Route to race management page.'''
    frequencies = [node.frequency for node in INTERFACE.nodes]
    nodes = []
    for idx, freq in enumerate(frequencies):
        if freq:
            nodes.append({
                'freq': freq,
                'index': idx
            })

    return render_template('run.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        led_enabled=(led_manager.isEnabled() or (CLUSTER and CLUSTER.hasRecEventsSecondaries())),
        vrx_enabled=vrx_manager.isEnabled(),
        num_nodes=RACE.num_nodes,
        nodes=nodes,
        cluster_has_secondaries=(CLUSTER and CLUSTER.hasSecondaries()))

@APP.route('/current')
def render_current():
    '''Route to race management page.'''
    frequencies = [node.frequency for node in INTERFACE.nodes]
    nodes = []
    for idx, freq in enumerate(frequencies):
        if freq:
            nodes.append({
                'freq': freq,
                'index': idx
            })

    return render_template('current.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        num_nodes=RACE.num_nodes,
        nodes=nodes,
        cluster_has_secondaries=(CLUSTER and CLUSTER.hasSecondaries()))

@APP.route('/marshal')
@requires_auth
def render_marshal():
    '''Route to race management page.'''
    return render_template('marshal.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        num_nodes=RACE.num_nodes)

@APP.route('/format')
@requires_auth
def render_format():
    '''Route to settings page.'''
    return render_template('format.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        num_nodes=RACE.num_nodes, Debug=Config.GENERAL['DEBUG'])

@APP.route('/settings')
@requires_auth
def render_settings():
    '''Route to settings page.'''
    server_messages_formatted = ''
    if len(ui_server_messages):
        for key, item in ui_server_messages.items():
            message = '<li class="' + key
            if 'subclass' in item and item['subclass']:
                message += ' ' + key + '-' + item['subclass']
            if 'header' in item and item['header']:
                message += ' ' + item['header'].lower()
            message += '">'
            if 'header' in item and item['header']:
                message += '<strong>' + item['header'] + ':</strong> '
            message += item['message']
            message += '</li>'
            server_messages_formatted += message
    if Config.GENERAL['configFile'] == -1:
        server_messages_formatted += '<li class="config config-bad warning"><strong>' + __('Warning') + ': ' + '</strong>' + __('The config.json file is invalid. Falling back to default configuration.') + '<br />' + __('See <a href="/docs?d=User Guide.md#set-up-config-file">User Guide</a> for more information.') + '</li>'
    elif Config.GENERAL['configFile'] == 0:
        server_messages_formatted += '<li class="config config-none warning"><strong>' + __('Warning') + ': ' + '</strong>' + __('No configuration file was loaded. Falling back to default configuration.') + '<br />' + __('See <a href="/docs?d=User Guide.md#set-up-config-file">User Guide</a> for more information.') +'</li>'

    return render_template('settings.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        led_enabled=(led_manager.isEnabled() or (CLUSTER and CLUSTER.hasRecEventsSecondaries())),
        led_events_enabled=led_manager.isEnabled(),
        vrx_enabled=vrx_manager.isEnabled(),
        num_nodes=RACE.num_nodes,
        server_messages=server_messages_formatted,
        cluster_has_secondaries=(CLUSTER and CLUSTER.hasSecondaries()),
        node_fw_updatable=(INTERFACE.get_fwupd_serial_name()!=None),
        is_raspberry_pi=RHUtils.isSysRaspberryPi(),
        Debug=Config.GENERAL['DEBUG'])

@APP.route('/streams')
def render_stream():
    '''Route to stream index.'''
    return render_template('streams.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        num_nodes=RACE.num_nodes)

@APP.route('/stream/results')
def render_stream_results():
    '''Route to current race leaderboard stream.'''
    return render_template('streamresults.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        num_nodes=RACE.num_nodes)

@APP.route('/stream/node/<int:node_id>')
def render_stream_node(node_id):
    '''Route to single node overlay for streaming.'''
    if node_id <= RACE.num_nodes:
        return render_template('streamnode.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
            node_id=node_id-1
        )
    else:
        return False

@APP.route('/stream/class/<int:class_id>')
def render_stream_class(class_id):
    '''Route to class leaderboard display for streaming.'''
    return render_template('streamclass.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        class_id=class_id
    )

@APP.route('/stream/heat/<int:heat_id>')
def render_stream_heat(heat_id):
    '''Route to heat display for streaming.'''
    return render_template('streamheat.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        num_nodes=RACE.num_nodes,
        heat_id=heat_id
    )

@APP.route('/scanner')
@requires_auth
def render_scanner():
    '''Route to scanner page.'''

    return render_template('scanner.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        num_nodes=RACE.num_nodes)

@APP.route('/decoder')
@requires_auth
def render_decoder():
    '''Route to race management page.'''
    return render_template('decoder.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        num_nodes=RACE.num_nodes)

@APP.route('/imdtabler')
def render_imdtabler():
    '''Route to IMDTabler page.'''
    return render_template('imdtabler.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__)

@APP.route('/updatenodes')
@requires_auth
def render_updatenodes():
    '''Route to update nodes page.'''
    return render_template('updatenodes.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__, \
                           fw_src_str=getDefNodeFwUpdateUrl())

# Debug Routes

@APP.route('/hardwarelog')
@requires_auth
def render_hardwarelog():
    '''Route to hardware log page.'''
    return render_template('hardwarelog.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__)

@APP.route('/database')
@requires_auth
def render_database():
    '''Route to database page.'''
    return render_template('database.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__,
        pilots=RHData.get_pilots(),
        heats=RHData.get_heats(),
        heatnodes=RHData.get_heatNodes(),
        race_class=RHData.get_raceClasses(),
        savedraceMeta=RHData.get_savedRaceMetas(),
        savedraceLap=RHData.get_savedRaceLaps(),
        profiles=RHData.get_profiles(),
        race_format=RHData.get_raceFormats(),
        globalSettings=RHData.get_options())

@APP.route('/vrxstatus')
@requires_auth
def render_vrxstatus():
    '''Route to VRx status debug page.'''
    return render_template('vrxstatus.html', serverInfo=serverInfo, getOption=RHData.get_option, __=__)

# Documentation Viewer

@APP.route('/docs')
def render_viewDocs():
    '''Route to doc viewer.'''

    folderBase = '../../doc/'

    try:
        docfile = request.args.get('d')

        docfile = docfile.replace('../', '')

        docPath = folderBase + docfile

        language = RHData.get_option("currentLanguage")
        if language:
            translated_path = folderBase + language + '/' + docfile
            if os.path.isfile(translated_path):
                docPath = translated_path

        with io.open(docPath, 'r', encoding="utf-8") as f:
            doc = f.read()

        return templating.render_template('viewdocs.html',
            serverInfo=serverInfo,
            getOption=RHData.get_option,
            __=__,
            doc=doc
            )
    except Exception:
        logger.exception("Exception in render_template")
    return "Error rendering documentation"

@APP.route('/img/<path:imgfile>')
def render_viewImg(imgfile):
    '''Route to img called within doc viewer.'''

    folderBase = '../../doc/'
    folderImg = 'img/'

    imgfile = imgfile.replace('../', '')

    imgPath = folderBase + folderImg + imgfile

    language = RHData.get_option("currentLanguage")
    if language:
        translated_path = folderBase + language + '/' + folderImg + imgfile
        if os.path.isfile(translated_path):
            imgPath = translated_path

    if os.path.isfile(imgPath):
        return send_file(imgPath)
    else:
        abort(404)

# Redirect routes (Previous versions/Delta 5)
@APP.route('/race')
def redirect_race():
    return redirect("/run", code=301)

@APP.route('/heats')
def redirect_heats():
    return redirect("/event", code=301)

def start_background_threads(forceFlag=False):
    global BACKGROUND_THREADS_ENABLED
    if BACKGROUND_THREADS_ENABLED or forceFlag:
        BACKGROUND_THREADS_ENABLED = True
        INTERFACE.start()
        global HEARTBEAT_THREAD
        if HEARTBEAT_THREAD is None:
            HEARTBEAT_THREAD = gevent.spawn(heartbeat_thread_function)
            logger.debug('Heartbeat thread started')
        start_shutdown_button_thread()

def stop_background_threads():
    try:
        stop_shutdown_button_thread()
        if CLUSTER:
            CLUSTER.shutdown()
        global BACKGROUND_THREADS_ENABLED
        BACKGROUND_THREADS_ENABLED = False
        global HEARTBEAT_THREAD
        if HEARTBEAT_THREAD:
            logger.info('Stopping heartbeat thread')
            HEARTBEAT_THREAD.kill(block=True, timeout=0.5)
            HEARTBEAT_THREAD = None
        INTERFACE.stop()
    except Exception:
        logger.error("Error stopping background threads")

#
# Socket IO Events
#

@SOCKET_IO.on('connect')
@catchLogExceptionsWrapper
def connect_handler():
    '''Starts the interface and a heartbeat thread for rssi.'''
    logger.debug('Client connected')
    start_background_threads()
    #
    @catchLogExceptionsWrapper
    @copy_current_request_context
    def finish_connect_handler():
        # push initial data
        RHUI.emit_frontend_load(nobroadcast=True)
    # pause and spawn to make sure connection to browser is established
    gevent.spawn_later(0.050, finish_connect_handler)

@SOCKET_IO.on('disconnect')
def disconnect_handler():
    '''Emit disconnect event.'''
    logger.debug('Client disconnected')

# LiveTime compatible events

@SOCKET_IO.on('get_version')
@catchLogExceptionsWrapper
def on_get_version():
    session['LiveTime'] = True
    ver_parts = RELEASE_VERSION.split('.')
    return {'major': ver_parts[0], 'minor': ver_parts[1]}

@SOCKET_IO.on('get_timestamp')
@catchLogExceptionsWrapper
def on_get_timestamp():
    if RACE.race_status == RaceStatus.STAGING:
        now = RACE.start_time_monotonic
    else:
        now = monotonic()
    return {'timestamp': monotonic_to_epoch_millis(now)}

@SOCKET_IO.on('get_settings')
@catchLogExceptionsWrapper
def on_get_settings():
    return {'nodes': [{
        'frequency': node.frequency,
        'trigger_rssi': node.enter_at_level
        } for node in INTERFACE.nodes
    ]}

@SOCKET_IO.on('reset_auto_calibration')
@catchLogExceptionsWrapper
def on_reset_auto_calibration(_data):
    on_discard_laps()
    RACE.format = SECONDARY_RACE_FORMAT
    RHUI.emit_current_laps()
    RHUI.emit_race_status()
    on_stage_race()

# Cluster events



@SOCKET_IO.on('join_cluster')
@catchLogExceptionsWrapper
def on_join_cluster():
    RACE.format = SECONDARY_RACE_FORMAT
    RHUI.emit_current_laps()
    RHUI.emit_race_status()
    logger.info("Joined cluster")
    Events.trigger(Evt.CLUSTER_JOIN, {
                'message': __('Joined cluster')
                })

@SOCKET_IO.on('join_cluster_ex')
@catchLogExceptionsWrapper
def on_join_cluster_ex(data=None):
    global Server_secondary_mode
    prev_mode = Server_secondary_mode
    Server_secondary_mode = str(data.get('mode', SecondaryNode.SPLIT_MODE)) if data else None
    logger.info("Joined cluster" + ((" as '" + Server_secondary_mode + "' timer") \
                                    if Server_secondary_mode else ""))
    if Server_secondary_mode != SecondaryNode.MIRROR_MODE:  # mode is split timer
        try:  # if first time joining and DB contains races then backup DB and clear races
            if prev_mode is None and len(RHData.get_savedRaceMetas()) > 0:
                logger.info("Making database autoBkp and clearing races on split timer")
                RHData.backup_db_file(True, "autoBkp_")
                RHData.clear_race_data()
                reset_current_laps()
                RHUI.emit_current_laps()
                RHUI.emit_result_data()
                RHData.delete_old_db_autoBkp_files(Config.GENERAL['DB_AUTOBKP_NUM_KEEP'], \
                                                   "autoBkp_", "DB_AUTOBKP_NUM_KEEP")
        except:
            logger.exception("Error making db-autoBkp / clearing races on split timer")
        RACE.format = SECONDARY_RACE_FORMAT
        RHUI.emit_current_laps()
        RHUI.emit_race_status()
    Events.trigger(Evt.CLUSTER_JOIN, {
                'message': __('Joined cluster')
                })
    CLUSTER.emit_join_cluster_response(SOCKET_IO, serverInfoItems)

@SOCKET_IO.on('check_secondary_query')
@catchLogExceptionsWrapper
def on_check_secondary_query(_data):
    ''' Check-query received from primary; return response. '''
    payload = {
        'timestamp': monotonic_to_epoch_millis(monotonic())
    }
    SOCKET_IO.emit('check_secondary_response', payload)

@SOCKET_IO.on('cluster_event_trigger')
@catchLogExceptionsWrapper
def on_cluster_event_trigger(data):
    ''' Received event trigger from primary. '''

    evtName = data['evt_name']
    evtArgs = json.loads(data['evt_args']) if 'evt_args' in data else None

    # set mirror timer state
    if Server_secondary_mode == SecondaryNode.MIRROR_MODE:
        if evtName == Evt.RACE_STAGE:
            RACE.race_status = RaceStatus.STAGING
            RACE.results = None
            if led_manager.isEnabled():
                if 'race_node_colors' in evtArgs and isinstance(evtArgs['race_node_colors'], list):
                    led_manager.setDisplayColorCache(evtArgs['race_node_colors'])
                else:
                    RHData.set_option('ledColorMode', 0)
        elif evtName == Evt.RACE_START:
            RACE.race_status = RaceStatus.RACING
        elif evtName == Evt.RACE_STOP:
            RACE.race_status = RaceStatus.DONE
        elif evtName == Evt.LAPS_CLEAR:
            RACE.race_status = RaceStatus.READY
        elif evtName == Evt.RACE_LAP_RECORDED:
            RACE.results = evtArgs['results']

    evtArgs.pop('RACE', None) # remove race if exists

    if evtName not in [Evt.STARTUP, Evt.LED_SET_MANUAL]:
        Events.trigger(evtName, evtArgs)
    # special handling for LED Control via primary timer
    elif 'effect' in evtArgs and led_manager.isEnabled():
        led_manager.setEventEffect(Evt.LED_MANUAL, evtArgs['effect'])


@SOCKET_IO.on('cluster_message_ack')
@catchLogExceptionsWrapper
def on_cluster_message_ack(data):
    ''' Received message acknowledgement from primary. '''
    if ClusterSendAckQueueObj:
        messageType = str(data.get('messageType')) if data else None
        messagePayload = data.get('messagePayload') if data else None
        ClusterSendAckQueueObj.ack(messageType, messagePayload)
    else:
        logger.warning("Received 'on_cluster_message_ack' message with no ClusterSendAckQueueObj setup")

# RotorHazard events

@SOCKET_IO.on('load_data')
@catchLogExceptionsWrapper
def on_load_data(data):
    '''Allow pages to load needed data'''
    load_types = data['load_types']
    for load_type in load_types:
        if isinstance(load_type, dict):
            if load_type['type'] == 'ui':
                RHUI.emit_ui(load_type['value'], nobroadcast=True)
        elif load_type == 'node_data':
            RHUI.emit_node_data(nobroadcast=True)
        elif load_type == 'environmental_data':
            RHUI.emit_environmental_data(nobroadcast=True)
        elif load_type == 'frequency_data':
            RHUI.emit_frequency_data(nobroadcast=True)
            if Use_imdtabler_jar_flag:
                heartbeat_thread_function.imdtabler_flag = True
        elif load_type == 'heat_data':
            RHUI.emit_heat_data(nobroadcast=True)
        elif load_type == 'class_data':
            RHUI.emit_class_data(nobroadcast=True)
        elif load_type == 'format_data':
            RHUI.emit_format_data(nobroadcast=True)
        elif load_type == 'pilot_data':
            RHUI.emit_pilot_data(nobroadcast=True)
        elif load_type == 'result_data':
            RHUI.emit_result_data(nobroadcast=True)
        elif load_type == 'node_tuning':
            RHUI.emit_node_tuning(nobroadcast=True)
        elif load_type == 'enter_and_exit_at_levels':
            RHUI.emit_enter_and_exit_at_levels(nobroadcast=True)
        elif load_type == 'start_thresh_lower_amount':
            RHUI.emit_start_thresh_lower_amount(nobroadcast=True)
        elif load_type == 'start_thresh_lower_duration':
            RHUI.emit_start_thresh_lower_duration(nobroadcast=True)
        elif load_type == 'min_lap':
            RHUI.emit_min_lap(nobroadcast=True)
        elif load_type == 'action_setup':
            RHUI.emit_action_setup(EventActionsObj, nobroadcast=True)
        elif load_type == 'event_actions':
            RHUI.emit_event_actions(nobroadcast=True)
        elif load_type == 'leaderboard':
            RHUI.emit_current_leaderboard(nobroadcast=True)
        elif load_type == 'current_laps':
            RHUI.emit_current_laps(nobroadcast=True)
        elif load_type == 'race_status':
            RHUI.emit_race_status(nobroadcast=True)
        elif load_type == 'current_heat':
            RHUI.emit_current_heat(nobroadcast=True)
        elif load_type == 'race_list':
            RHUI.emit_race_list(nobroadcast=True)
        elif load_type == 'language':
            RHUI.emit_language(nobroadcast=True)
        elif load_type == 'all_languages':
            RHUI.emit_all_languages(nobroadcast=True)
        elif load_type == 'led_effect_setup':
            emit_led_effect_setup()
        elif load_type == 'led_effects':
            emit_led_effects()
        elif load_type == 'callouts':
            RHUI.emit_callouts()
        elif load_type == 'imdtabler_page':
            RHUI.emit_imdtabler_page(IMDTABLER_JAR_NAME, Use_imdtabler_jar_flag, nobroadcast=True)
        elif load_type == 'vrx_list':
            RHUI.emit_vrx_list(nobroadcast=True)
        elif load_type == 'backups_list':
            on_list_backups()
        elif load_type == 'exporter_list':
            RHUI.emit_exporter_list()
        elif load_type == 'heatgenerator_list':
            RHUI.emit_heatgenerator_list()
        elif load_type == 'cluster_status':
            RHUI.emit_cluster_status()
        elif load_type == 'hardware_log_init':
            emit_current_log_file_to_socket()
        else:
            logger.warning('Called undefined load type: {}'.format(load_type))

@SOCKET_IO.on('broadcast_message')
@catchLogExceptionsWrapper
def on_broadcast_message(data):
    RHUI.emit_priority_message(data['message'], data['interrupt'])

# Settings socket io events

@SOCKET_IO.on('set_frequency')
@catchLogExceptionsWrapper
def on_set_frequency(data):
    '''Set node frequency.'''
    if CLUSTER:
        CLUSTER.emitToSplits('set_frequency', data)
    if isinstance(data, string_types): # LiveTime compatibility
        data = json.loads(data)
    node_index = data['node']
    frequency = int(data['frequency'])
    band = str(data['band']) if 'band' in data and data['band'] != None else None
    channel = int(data['channel']) if 'channel' in data and data['channel'] != None else None

    if node_index < 0 or node_index >= RACE.num_nodes:
        logger.info('Unable to set frequency ({0}) on node {1}; node index out of range'.format(frequency, node_index+1))
        return

    profile = RACE.profile
    freqs = json.loads(profile.frequencies)

    # handle case where more nodes were added
    while node_index >= len(freqs["f"]):
        freqs["b"].append(None)
        freqs["c"].append(None)
        freqs["f"].append(RHUtils.FREQUENCY_ID_NONE)

    freqs["b"][node_index] = band
    freqs["c"][node_index] = channel
    freqs["f"][node_index] = frequency
    logger.info('Frequency set: Node {0} B:{1} Ch:{2} Freq:{3}'.format(node_index+1, band, channel, frequency))

    RHData.alter_profile({
        'profile_id': profile.id,
        'frequencies': freqs
        })
    RACE.profile = profile

    INTERFACE.set_frequency(node_index, frequency)

    RACE.clear_results()

    Events.trigger(Evt.FREQUENCY_SET, {
        'nodeIndex': node_index,
        'frequency': frequency,
        'band': band,
        'channel': channel
        })

    RHUI.emit_frequency_data()
    if Use_imdtabler_jar_flag:
        heartbeat_thread_function.imdtabler_flag = True

@SOCKET_IO.on('set_frequency_preset')
@catchLogExceptionsWrapper
def on_set_frequency_preset(data):
    ''' Apply preset frequencies '''
    if CLUSTER:
        CLUSTER.emitToSplits('set_frequency_preset', data)
    bands = []
    channels = []
    freqs = []
    if data['preset'] == 'All-N1':
        profile_freqs = json.loads(RACE.profile.frequencies)
        for _idx in range(RACE.num_nodes):
            bands.append(profile_freqs["b"][0])
            channels.append(profile_freqs["c"][0])
            freqs.append(profile_freqs["f"][0])
    else:
        if data['preset'] == 'RB-4':
            bands = ['R', 'R', 'R', 'R']
            channels = [1, 3, 6, 7]
            freqs = [5658, 5732, 5843, 5880]
        elif data['preset'] == 'RB-8':
            bands = ['R', 'R', 'R', 'R', 'R', 'R', 'R', 'R']
            channels = [1, 2, 3, 4, 5, 6, 7, 8]
            freqs = [5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917]
        elif data['preset'] == 'IMD5C':
            bands = ['R', 'R', 'F', 'F', 'E']
            channels = [1, 2, 2, 4, 5]
            freqs = [5658, 5695, 5760, 5800, 5885]
        else: #IMD6C is default
            bands = ['R', 'R', 'F', 'F', 'R', 'R']
            channels = [1, 2, 2, 4, 7, 8]
            freqs = [5658, 5695, 5760, 5800, 5880, 5917]
        while RACE.num_nodes > len(bands):
            bands.append(RHUtils.FREQUENCY_ID_NONE)
        while RACE.num_nodes > len(channels):
            channels.append(RHUtils.FREQUENCY_ID_NONE)
        while RACE.num_nodes > len(freqs):
            freqs.append(RHUtils.FREQUENCY_ID_NONE)

    payload = {
        "b": bands,
        "c": channels,
        "f": freqs
    }

    set_all_frequencies(payload)
    RHUI.emit_frequency_data()
    if Use_imdtabler_jar_flag:
        heartbeat_thread_function.imdtabler_flag = True
    hardware_set_all_frequencies(payload)

def set_all_frequencies(freqs):
    ''' Set frequencies for all nodes (but do not update hardware) '''
    # Set DB
    profile = RACE.profile
    profile_freqs = json.loads(profile.frequencies)

    for idx in range(RACE.num_nodes):
        profile_freqs["b"][idx] = freqs["b"][idx]
        profile_freqs["c"][idx] = freqs["c"][idx]
        profile_freqs["f"][idx] = freqs["f"][idx]
        logger.info('Frequency set: Node {0} B:{1} Ch:{2} Freq:{3}'.format(idx+1, freqs["b"][idx], freqs["c"][idx], freqs["f"][idx]))

    RHData.alter_profile({
        'profile_id': profile.id,
        'frequencies': profile_freqs
        })
    RACE.profile = profile

def hardware_set_all_frequencies(freqs):
    '''do hardware update for frequencies'''
    logger.debug("Sending frequency values to nodes: " + str(freqs["f"]))
    for idx in range(RACE.num_nodes):
        INTERFACE.set_frequency(idx, freqs["f"][idx])

        RACE.clear_results()

        Events.trigger(Evt.FREQUENCY_SET, {
            'nodeIndex': idx,
            'frequency': freqs["f"][idx],
            'band': freqs["b"][idx],
            'channel': freqs["c"][idx]
            })

@catchLogExceptionsWrapper
def restore_node_frequency(node_index):
    ''' Restore frequency for given node index (update hardware) '''
    gevent.sleep(0.250)  # pause to get clear of heartbeat actions for scanner
    profile_freqs = json.loads(RACE.profile.frequencies)
    freq = profile_freqs["f"][node_index]
    INTERFACE.set_frequency(node_index, freq)
    logger.info('Frequency restored: Node {0} Frequency {1}'.format(node_index+1, freq))

@SOCKET_IO.on('set_enter_at_level')
@catchLogExceptionsWrapper
def on_set_enter_at_level(data):
    '''Set node enter-at level.'''
    node_index = data['node']
    enter_at_level = data['enter_at_level']

    if node_index < 0 or node_index >= RACE.num_nodes:
        logger.info('Unable to set enter-at ({0}) on node {1}; node index out of range'.format(enter_at_level, node_index+1))
        return

    if not enter_at_level:
        logger.info('Node enter-at set null; getting from node: Node {0}'.format(node_index+1))
        enter_at_level = INTERFACE.nodes[node_index].enter_at_level

    profile = RACE.profile
    enter_ats = json.loads(profile.enter_ats)

    # handle case where more nodes were added
    while node_index >= len(enter_ats["v"]):
        enter_ats["v"].append(None)

    enter_ats["v"][node_index] = enter_at_level

    RHData.alter_profile({
        'profile_id': profile.id,
        'enter_ats': enter_ats
        })
    RACE.profile = profile

    INTERFACE.set_enter_at_level(node_index, enter_at_level)

    Events.trigger(Evt.ENTER_AT_LEVEL_SET, {
        'nodeIndex': node_index,
        'enter_at_level': enter_at_level,
        })

    logger.info('Node enter-at set: Node {0} Level {1}'.format(node_index+1, enter_at_level))

@SOCKET_IO.on('set_exit_at_level')
@catchLogExceptionsWrapper
def on_set_exit_at_level(data):
    '''Set node exit-at level.'''
    node_index = data['node']
    exit_at_level = data['exit_at_level']

    if node_index < 0 or node_index >= RACE.num_nodes:
        logger.info('Unable to set exit-at ({0}) on node {1}; node index out of range'.format(exit_at_level, node_index+1))
        return

    if not exit_at_level:
        logger.info('Node exit-at set null; getting from node: Node {0}'.format(node_index+1))
        exit_at_level = INTERFACE.nodes[node_index].exit_at_level

    profile = RACE.profile
    exit_ats = json.loads(profile.exit_ats)

    # handle case where more nodes were added
    while node_index >= len(exit_ats["v"]):
        exit_ats["v"].append(None)

    exit_ats["v"][node_index] = exit_at_level

    RHData.alter_profile({
        'profile_id': profile.id,
        'exit_ats': exit_ats
        })
    RACE.profile = profile

    INTERFACE.set_exit_at_level(node_index, exit_at_level)

    Events.trigger(Evt.EXIT_AT_LEVEL_SET, {
        'nodeIndex': node_index,
        'exit_at_level': exit_at_level,
        })

    logger.info('Node exit-at set: Node {0} Level {1}'.format(node_index+1, exit_at_level))

def hardware_set_all_enter_ats(enter_at_levels):
    '''send update to nodes'''
    logger.debug("Sending enter-at values to nodes: " + str(enter_at_levels))
    for idx in range(RACE.num_nodes):
        if enter_at_levels[idx]:
            INTERFACE.set_enter_at_level(idx, enter_at_levels[idx])
        else:
            on_set_enter_at_level({
                'node': idx,
                'enter_at_level': INTERFACE.nodes[idx].enter_at_level
                })

def hardware_set_all_exit_ats(exit_at_levels):
    '''send update to nodes'''
    logger.debug("Sending exit-at values to nodes: " + str(exit_at_levels))
    for idx in range(RACE.num_nodes):
        if exit_at_levels[idx]:
            INTERFACE.set_exit_at_level(idx, exit_at_levels[idx])
        else:
            on_set_exit_at_level({
                'node': idx,
                'exit_at_level': INTERFACE.nodes[idx].exit_at_level
                })

@SOCKET_IO.on("set_start_thresh_lower_amount")
@catchLogExceptionsWrapper
def on_set_start_thresh_lower_amount(data):
    start_thresh_lower_amount = data['start_thresh_lower_amount']
    RHData.set_option("startThreshLowerAmount", start_thresh_lower_amount)
    logger.info("set start_thresh_lower_amount to %s percent" % start_thresh_lower_amount)
    RHUI.emit_start_thresh_lower_amount(noself=True)

@SOCKET_IO.on("set_start_thresh_lower_duration")
@catchLogExceptionsWrapper
def on_set_start_thresh_lower_duration(data):
    start_thresh_lower_duration = data['start_thresh_lower_duration']
    RHData.set_option("startThreshLowerDuration", start_thresh_lower_duration)
    logger.info("set start_thresh_lower_duration to %s seconds" % start_thresh_lower_duration)
    RHUI.emit_start_thresh_lower_duration(noself=True)

@SOCKET_IO.on('set_language')
@catchLogExceptionsWrapper
def on_set_language(data):
    '''Set interface language.'''
    RHData.set_option('currentLanguage', data['language'])

@SOCKET_IO.on('cap_enter_at_btn')
@catchLogExceptionsWrapper
def on_cap_enter_at_btn(data):
    '''Capture enter-at level.'''
    node_index = data['node_index']
    if INTERFACE.start_capture_enter_at_level(node_index):
        logger.info('Starting capture of enter-at level for node {0}'.format(node_index+1))

@SOCKET_IO.on('cap_exit_at_btn')
@catchLogExceptionsWrapper
def on_cap_exit_at_btn(data):
    '''Capture exit-at level.'''
    node_index = data['node_index']
    if INTERFACE.start_capture_exit_at_level(node_index):
        logger.info('Starting capture of exit-at level for node {0}'.format(node_index+1))

@SOCKET_IO.on('set_scan')
@catchLogExceptionsWrapper
def on_set_scan(data):
    global HEARTBEAT_DATA_RATE_FACTOR
    node_index = data['node']
    minScanFreq = data['min_scan_frequency']
    maxScanFreq = data['max_scan_frequency']
    maxScanInterval = data['max_scan_interval']
    minScanInterval = data['min_scan_interval']
    scanZoom = data['scan_zoom']
    node = INTERFACE.nodes[node_index]
    node.set_scan_interval(minScanFreq, maxScanFreq, maxScanInterval, minScanInterval, scanZoom)
    if node.scan_enabled:
        HEARTBEAT_DATA_RATE_FACTOR = 50
    else:
        HEARTBEAT_DATA_RATE_FACTOR = 5
        gevent.sleep(0.100)  # pause/spawn to get clear of heartbeat actions for scanner
        gevent.spawn(restore_node_frequency, node_index)

@SOCKET_IO.on('add_heat')
@catchLogExceptionsWrapper
def on_add_heat(data=None):
    '''Adds the next available heat number to the database.'''
    if data and 'class' in data:
        RHData.add_heat(init={'class_id': data['class']})
    else:
        RHData.add_heat()
    RHUI.emit_heat_data()

@SOCKET_IO.on('duplicate_heat')
@catchLogExceptionsWrapper
def on_duplicate_heat(data):
    RHData.duplicate_heat(data['heat'])
    RHUI.emit_heat_data()

@SOCKET_IO.on('alter_heat')
@catchLogExceptionsWrapper
def on_alter_heat(data):
    '''Update heat.'''
    heat, altered_race_list = RHData.alter_heat(data)
    if RACE.current_heat == heat.id:  # if current heat was altered then update heat data
        set_current_heat_data(heat.id, silent=True)
    RHUI.emit_heat_data(noself=True)
    if ('note' in data or 'pilot' in data or 'class' in data) and len(altered_race_list):
        RHUI.emit_result_data() # live update rounds page
        message = __('Alterations made to heat: {0}').format(heat.displayname())
        RHUI.emit_priority_message(message, False)

@SOCKET_IO.on('delete_heat')
@catchLogExceptionsWrapper
def on_delete_heat(data):
    '''Delete heat.'''
    global LAST_RACE
    heat_id = data['heat']
    result = RHData.delete_heat(heat_id)
    if result is not None:
        if LAST_RACE and LAST_RACE.current_heat == result:
            LAST_RACE = None  # if last-race heat deleted then clear last race
        if RACE.current_heat == result:  # if current heat was deleted then drop to practice mode (avoids dynamic heat calculation)
            set_current_heat_data(RHUtils.HEAT_ID_NONE)
        RHUI.emit_heat_data()

@SOCKET_IO.on('add_race_class')
@catchLogExceptionsWrapper
def on_add_race_class():
    '''Adds the next available pilot id number in the database.'''
    RHData.add_raceClass()
    RHUI.emit_class_data()
    RHUI.emit_heat_data() # Update class selections in heat displays

@SOCKET_IO.on('duplicate_race_class')
@catchLogExceptionsWrapper
def on_duplicate_race_class(data):
    '''Adds new race class by duplicating an existing one.'''
    RHData.duplicate_raceClass(data['class'])
    RHUI.emit_class_data()
    RHUI.emit_heat_data()

@SOCKET_IO.on('alter_race_class')
@catchLogExceptionsWrapper
def on_alter_race_class(data):
    '''Update race class.'''
    race_class, altered_race_list = RHData.alter_raceClass(data)

    if ('class_format' in data or 'class_name' in data or 'win_condition' in data) and len(altered_race_list):
        RHUI.emit_result_data() # live update rounds page
        message = __('Alterations made to race class: {0}').format(race_class.displayname())
        RHUI.emit_priority_message(message, False)

    RHUI.emit_class_data(noself=True)
    if 'class_name' in data:
        RHUI.emit_heat_data() # Update class names in heat displays
    if 'class_format' in data:
        RHUI.emit_current_heat(noself=True) # in case race operator is a different client, update locked format dropdown

@SOCKET_IO.on('delete_class')
@catchLogExceptionsWrapper
def on_delete_class(data):
    '''Delete class.'''
    result = RHData.delete_raceClass(data['class'])
    if result:
        RHUI.emit_class_data()
        RHUI.emit_heat_data()

@SOCKET_IO.on('add_pilot')
@catchLogExceptionsWrapper
def on_add_pilot():
    '''Adds the next available pilot id number in the database.'''
    RHData.add_pilot()
    RHUI.emit_pilot_data()

@SOCKET_IO.on('alter_pilot')
@catchLogExceptionsWrapper
def on_alter_pilot(data):
    '''Update pilot.'''
    _pilot, race_list = RHData.alter_pilot(data)

    RHUI.emit_pilot_data(noself=True) # Settings page, new pilot settings

    if 'callsign' in data or 'team_name' in data:
        RHUI.emit_heat_data() # Settings page, new pilot callsign in heats
        if len(race_list):
            RHUI.emit_result_data() # live update rounds page
    if 'phonetic' in data:
        RHUI.emit_heat_data() # Settings page, new pilot phonetic in heats. Needed?

    RACE.clear_results() # refresh current leaderboard

@SOCKET_IO.on('delete_pilot')
@catchLogExceptionsWrapper
def on_delete_pilot(data):
    '''Delete pilot.'''
    result = RHData.delete_pilot(data['pilot'])

    if result:
        RHUI.emit_pilot_data()
        RHUI.emit_heat_data()

@SOCKET_IO.on('add_profile')
@catchLogExceptionsWrapper
def on_add_profile():
    '''Adds new profile (frequency set) in the database.'''
    source_profile = RACE.profile
    new_profile = RHData.duplicate_profile(source_profile.id)

    on_set_profile({ 'profile': new_profile.id })

@SOCKET_IO.on('alter_profile')
@catchLogExceptionsWrapper
def on_alter_profile(data):
    ''' update profile '''
    profile = RACE.profile
    data['profile_id'] = profile.id
    profile = RHData.alter_profile(data)
    RACE.profile = profile

    RHUI.emit_node_tuning(noself=True)

@SOCKET_IO.on('delete_profile')
@catchLogExceptionsWrapper
def on_delete_profile():
    '''Delete profile'''
    profile = RACE.profile
    result = RHData.delete_profile(profile.id)

    if result:
        first_profile_id = RHData.get_first_profile().id
        RHData.set_option("currentProfile", first_profile_id)
        on_set_profile({ 'profile': first_profile_id })

@SOCKET_IO.on("set_profile")
@catchLogExceptionsWrapper
def on_set_profile(data, emit_vals=True):
    ''' set current profile '''
    profile_val = int(data['profile'])
    profile = RHData.get_profile(profile_val)
    if profile:
        RHData.set_option("currentProfile", data['profile'])
        logger.info("Set Profile to '%s'" % profile_val)
        RACE.profile = profile
        # set freqs, enter_ats, and exit_ats
        freqs = json.loads(profile.frequencies)
        moreNodesFlag = False
        if RACE.num_nodes > len(freqs["b"]) or RACE.num_nodes > len(freqs["c"]) or \
                                        RACE.num_nodes > len(freqs["f"]):
            moreNodesFlag = True
            while RACE.num_nodes > len(freqs["b"]):
                freqs["b"].append(RHUtils.FREQUENCY_ID_NONE)
            while RACE.num_nodes > len(freqs["c"]):
                freqs["c"].append(RHUtils.FREQUENCY_ID_NONE)
            while RACE.num_nodes > len(freqs["f"]):
                freqs["f"].append(RHUtils.FREQUENCY_ID_NONE)

        if profile.enter_ats:
            enter_ats_loaded = json.loads(profile.enter_ats)
            enter_ats = enter_ats_loaded["v"]
            if RACE.num_nodes > len(enter_ats):
                moreNodesFlag = True
                while RACE.num_nodes > len(enter_ats):
                    enter_ats.append(None)
        else: #handle null data by copying in hardware values
            enter_at_levels = {}
            enter_at_levels["v"] = [node.enter_at_level for node in INTERFACE.nodes]
            RHData.alter_profile({'profile_id': profile_val, 'enter_ats': enter_at_levels})
            enter_ats = enter_at_levels["v"]

        if profile.exit_ats:
            exit_ats_loaded = json.loads(profile.exit_ats)
            exit_ats = exit_ats_loaded["v"]
            if RACE.num_nodes > len(exit_ats):
                moreNodesFlag = True
                while RACE.num_nodes > len(exit_ats):
                    exit_ats.append(None)
        else: #handle null data by copying in hardware values
            exit_at_levels = {}
            exit_at_levels["v"] = [node.exit_at_level for node in INTERFACE.nodes]
            RHData.alter_profile({'profile_id': profile_val,'exit_ats': exit_at_levels})
            exit_ats = exit_at_levels["v"]

        # if added nodes detected then update profile values in database
        if moreNodesFlag:
            logger.info("Updating profile '%s' in DB to account for added nodes" % profile_val)
            RHData.alter_profile({'profile_id': profile_val, 'frequencies': freqs,
                                  'enter_ats': enter_ats_loaded, 'exit_ats': exit_ats_loaded})

        RACE.profile = profile
        Events.trigger(Evt.PROFILE_SET, {
            'profile_id': profile_val,
            })

        if emit_vals:
            RHUI.emit_node_tuning()
            RHUI.emit_enter_and_exit_at_levels()
            RHUI.emit_frequency_data()
            if Use_imdtabler_jar_flag:
                heartbeat_thread_function.imdtabler_flag = True

        hardware_set_all_frequencies(freqs)
        hardware_set_all_enter_ats(enter_ats)
        hardware_set_all_exit_ats(exit_ats)

    else:
        logger.warning('Invalid set_profile value: ' + str(profile_val))

@SOCKET_IO.on('alter_race')
@catchLogExceptionsWrapper
def on_alter_race(data):
    '''Update race (retroactively via marshaling).'''

    _race_meta, new_heat = RHData.reassign_savedRaceMeta_heat(data['race_id'], data['heat_id'])

    message = __('A race has been reassigned to {0}').format(new_heat.displayname())
    RHUI.emit_priority_message(message, False)

    RHUI.emit_race_list(nobroadcast=True)
    RHUI.emit_result_data()

@SOCKET_IO.on('backup_database')
@catchLogExceptionsWrapper
def on_backup_database():
    '''Backup database.'''
    bkp_name = RHData.backup_db_file(True)  # make copy of DB file

    # read DB data and convert to Base64
    with open(bkp_name, mode='rb') as file_obj:
        file_content = file_obj.read()
    if hasattr(base64, "encodebytes"):
        file_content = base64.encodebytes(file_content).decode()
    else:
        file_content = base64.encodestring(file_content)  #pylint: disable=deprecated-method,undefined-variable

    emit_payload = {
        'file_name': os.path.basename(bkp_name),
        'file_data' : file_content
    }

    Events.trigger(Evt.DATABASE_BACKUP, {
        'file_name': emit_payload['file_name'],
        })

    emit('database_bkp_done', emit_payload)
    on_list_backups()

@SOCKET_IO.on('list_backups')
@catchLogExceptionsWrapper
def on_list_backups():
    '''List database files in db_bkp'''

    if not os.path.exists(DB_BKP_DIR_NAME):
        emit_payload = {
            'backup_files': None
        }
    else:
        files = []
        for (_, _, filenames) in os.walk(DB_BKP_DIR_NAME):
            files.extend(filenames)
            break

        emit_payload = {
            'backup_files': files
        }

    emit('backups_list', emit_payload)

def restore_database_file(db_file_name):
    global RACE
    global LAST_RACE
    RHData.close()
    RACE = RHRace.RHRace() # Reset all RACE values
    RACE.num_nodes = len(INTERFACE.nodes)  # restore number of nodes
    LAST_RACE = RACE
    try:
        RHData.recover_database(db_file_name)
        reset_current_laps()
        clean_results_cache()
        expand_heats()
        raceformat_id = RHData.get_optionInt('currentFormat')
        RACE.format = RHData.get_raceFormat(raceformat_id)
        RHUI.emit_current_laps()
        success = True
    except Exception as ex:
        logger.warning('Clearing all data after recovery failure:  ' + str(ex))
        db_reset()
        success = False
    init_race_state()
    init_interface_state()
    return success

@SOCKET_IO.on('restore_database')
@catchLogExceptionsWrapper
def on_restore_database(data):
    '''Restore database.'''
    success = None
    if 'backup_file' in data:
        backup_file = data['backup_file']
        backup_path = DB_BKP_DIR_NAME + '/' + backup_file

        if os.path.exists(backup_path):
            logger.info('Found {0}: starting restoration...'.format(backup_file))
            success = restore_database_file(backup_path)
            Events.trigger(Evt.DATABASE_RESTORE, {
                'file_name': backup_file,
                })

            SOCKET_IO.emit('database_restore_done')
        else:
            logger.warning('Unable to restore {0}: File does not exist'.format(backup_file))
            success = False

    if success == False:
        message = __('Database recovery failed for: {0}').format(backup_file)
        RHUI.emit_priority_message(message, False, nobroadcast=True)

@SOCKET_IO.on('delete_database')
@catchLogExceptionsWrapper
def on_delete_database_file(data):
    '''Restore database.'''
    if 'backup_file' in data:
        backup_file = data['backup_file']
        backup_path = DB_BKP_DIR_NAME + '/' + backup_file

        if os.path.exists(backup_path):
            logger.info('Deleting backup file {0}'.format(backup_file))
            os.remove(backup_path)

            emit_payload = {
                'file_name': backup_file
            }

            Events.trigger(Evt.DATABASE_DELETE_BACKUP, {
                'file_name': backup_file,
                })

            SOCKET_IO.emit('database_delete_done', emit_payload)
            on_list_backups()
        else:
            logger.warning('Unable to delete {0}: File does not exist'.format(backup_file))

@SOCKET_IO.on('reset_database')
@catchLogExceptionsWrapper
def on_reset_database(data):
    '''Reset database.'''
    PageCache.set_valid(False)

    reset_type = data['reset_type']
    if reset_type == 'races':
        RHData.clear_race_data()
        reset_current_laps()
    elif reset_type == 'heats':
        RHData.reset_heats()
        RHData.clear_race_data()
        reset_current_laps()
    elif reset_type == 'classes':
        RHData.reset_heats()
        RHData.reset_raceClasses()
        RHData.clear_race_data()
        reset_current_laps()
    elif reset_type == 'pilots':
        RHData.reset_pilots()
        RHData.reset_heats()
        RHData.clear_race_data()
        reset_current_laps()
    elif reset_type == 'all':
        RHData.reset_pilots()
        RHData.reset_heats()
        RHData.reset_raceClasses()
        RHData.clear_race_data()
        reset_current_laps()
    elif reset_type == 'formats':
        RHData.clear_race_data()
        reset_current_laps()
        RHData.reset_raceFormats()
        RACE.format = RHData.get_first_raceFormat()
    finalize_current_heat_set(RHData.get_first_safe_heat_id())
    RHUI.emit_heat_data()
    RHUI.emit_pilot_data()
    RHUI.emit_format_data()
    RHUI.emit_class_data()
    RHUI.emit_current_laps()
    RHUI.emit_result_data()
    emit('reset_confirm')

    Events.trigger(Evt.DATABASE_RESET)

@SOCKET_IO.on('export_database')
@catchLogExceptionsWrapper
def on_export_database_file(data):
    '''Run the selected Exporter'''
    exporter = data['exporter']

    if export_manager.hasExporter(exporter):
        # do export
        logger.info('Exporting data via {0}'.format(exporter))
        export_result = export_manager.export(exporter)

        if export_result != False:
            try:
                emit_payload = {
                    'filename': 'RotorHazard Export ' + datetime.now().strftime('%Y%m%d_%H%M%S') + ' ' + exporter + '.' + export_result['ext'],
                    'encoding': export_result['encoding'],
                    'data' : export_result['data']
                }
                emit('exported_data', emit_payload)

                Events.trigger(Evt.DATABASE_EXPORT, export_result)
            except Exception:
                logger.exception("Error downloading export file")
                RHUI.emit_priority_message(__('Data export failed. (See log)'), False, nobroadcast=True)
        else:
            logger.warning('Failed exporting data: exporter returned no data')
            RHUI.emit_priority_message(__('Data export failed. (See log)'), False, nobroadcast=True)

        return

    logger.error('Data exporter "{0}" not found'.format(exporter))
    RHUI.emit_priority_message(__('Data export failed. (See log)'), False, nobroadcast=True)

@SOCKET_IO.on('generate_heats_v2')
@catchLogExceptionsWrapper
def on_generate_heats_v2(data):
    '''Run the selected Generator'''
    available_nodes = 0
    profile_freqs = json.loads(RACE.profile.frequencies)
    for node_index in range(RACE.num_nodes):
        if profile_freqs["f"][node_index] != RHUtils.FREQUENCY_ID_NONE:
            available_nodes += 1

    generate_args = {
        'input_class': data['input_class'],
        'output_class': data['output_class'],
        # 'suffix': data['suffix'],
        # 'pilots_per_heat': int(data['pilots_per_heat']),
        'available_nodes': available_nodes
        }
    generator = data['generator']

    if heatgenerate_manager.hasGenerator(generator):
        generatorObj = heatgenerate_manager.getGenerator(generator)

        # do export
        logger.info('Generating heats via {0}'.format(generatorObj.label))
        generate_result = heatgenerate_manager.generate(generator, generate_args)

        if generate_result != False:
            RHUI.emit_priority_message(__('Generated heats via {0}'.format(generatorObj.label)), False, nobroadcast=True)
            RHUI.emit_heat_data()
            RHUI.emit_class_data()
            Events.trigger(Evt.HEAT_GENERATE)
        else:
            logger.warning('Failed generating heats: generator returned no data')
            RHUI.emit_priority_message(__('Heat generation failed. (See log)'), False, nobroadcast=True)

        return

    logger.error('Heat generator "{0}" not found'.format(generator))
    RHUI.emit_priority_message(__('Heat generation failed. (See log)'), False, nobroadcast=True)

@SOCKET_IO.on('shutdown_pi')
@catchLogExceptionsWrapper
def on_shutdown_pi():
    '''Shutdown the raspberry pi.'''
    if  INTERFACE.send_shutdown_started_message():
        gevent.sleep(0.25)  # give shutdown-started message a chance to transmit to node
    if CLUSTER:
        CLUSTER.emit('shutdown_pi')
    RHUI.emit_priority_message(__('Server has shut down.'), True, caller='shutdown')
    logger.info('Performing system shutdown')
    Events.trigger(Evt.SHUTDOWN)
    stop_background_threads()
    gevent.sleep(0.5)
    gevent.spawn(SOCKET_IO.stop)  # shut down flask http server
    if RHUtils.isSysRaspberryPi():
        gevent.sleep(0.1)
        logger.debug("Executing system command:  sudo shutdown now")
        log.wait_for_queue_empty()
        log.close_logging()
        os.system("sudo shutdown now")
    else:
        logger.warning("Not executing system shutdown command because not RPi")

@SOCKET_IO.on('reboot_pi')
@catchLogExceptionsWrapper
def on_reboot_pi():
    '''Reboot the raspberry pi.'''
    if CLUSTER:
        CLUSTER.emit('reboot_pi')
    RHUI.emit_priority_message(__('Server is rebooting.'), True, caller='shutdown')
    logger.info('Performing system reboot')
    Events.trigger(Evt.SHUTDOWN)
    stop_background_threads()
    gevent.sleep(0.5)
    gevent.spawn(SOCKET_IO.stop)  # shut down flask http server
    if RHUtils.isSysRaspberryPi():
        gevent.sleep(0.1)
        logger.debug("Executing system command:  sudo reboot now")
        log.wait_for_queue_empty()
        log.close_logging()
        os.system("sudo reboot now")
    else:
        logger.warning("Not executing system reboot command because not RPi")

@SOCKET_IO.on('kill_server')
@catchLogExceptionsWrapper
def on_kill_server():
    '''Shutdown this server.'''
    if CLUSTER:
        CLUSTER.emit('kill_server')
    RHUI.emit_priority_message(__('Server has stopped.'), True, caller='shutdown')
    logger.info('Killing RotorHazard server')
    Events.trigger(Evt.SHUTDOWN)
    stop_background_threads()
    gevent.sleep(0.5)
    gevent.spawn(SOCKET_IO.stop)  # shut down flask http server

@SOCKET_IO.on('download_logs')
@catchLogExceptionsWrapper
def on_download_logs(data):
    '''Download logs (as .zip file).'''
    zip_path_name = log.create_log_files_zip(logger, Config.CONFIG_FILE_NAME, DB_FILE_NAME)
    RHUtils.checkSetFileOwnerPi(log.LOGZIP_DIR_NAME)
    if zip_path_name:
        RHUtils.checkSetFileOwnerPi(zip_path_name)
        try:
            # read logs-zip file data and convert to Base64
            with open(zip_path_name, mode='rb') as file_obj:
                file_content = file_obj.read()
            if hasattr(base64, "encodebytes"):
                file_content = base64.encodebytes(file_content).decode()
            else:
                file_content = base64.encodestring(file_content)  #pylint: disable=deprecated-method,undefined-variable

            emit_payload = {
                'file_name': os.path.basename(zip_path_name),
                'file_data' : file_content
            }
            Events.trigger(Evt.DATABASE_BACKUP, {
                'file_name': emit_payload['file_name'],
                })
            SOCKET_IO.emit(data['emit_fn_name'], emit_payload)
        except Exception:
            logger.exception("Error downloading logs-zip file")

@SOCKET_IO.on("set_min_lap")
@catchLogExceptionsWrapper
def on_set_min_lap(data):
    min_lap = data['min_lap']
    RHData.set_option("MinLapSec", data['min_lap'])

    Events.trigger(Evt.MIN_LAP_TIME_SET, {
        'min_lap': min_lap,
        })

    logger.info("set min lap time to %s seconds" % min_lap)
    RHUI.emit_min_lap(noself=True)

@SOCKET_IO.on("set_min_lap_behavior")
@catchLogExceptionsWrapper
def on_set_min_lap_behavior(data):
    min_lap_behavior = int(data['min_lap_behavior'])
    RHData.set_option("MinLapBehavior", min_lap_behavior)

    Events.trigger(Evt.MIN_LAP_BEHAVIOR_SET, {
        'min_lap_behavior': min_lap_behavior,
        })

    logger.info("set min lap behavior to %s" % min_lap_behavior)
    RHUI.emit_min_lap(noself=True)

@SOCKET_IO.on("set_race_format")
@catchLogExceptionsWrapper
def on_set_race_format(data):
    ''' set current race_format '''
    if RACE.race_status == RaceStatus.READY: # prevent format change if race running
        race_format_val = data['race_format']
        RACE.format = RHData.get_raceFormat(race_format_val)
        RHUI.emit_current_laps()

        Events.trigger(Evt.RACE_FORMAT_SET, {
            'race_format': race_format_val,
            })

        RHUI.emit_race_status()
        logger.info("set race format to '%s' (%s)" % (RACE.format.name, RACE.format.id))
    else:
        RHUI.emit_priority_message(__('Format change prevented by active race: Stop and save/discard laps'), False, nobroadcast=True)
        logger.info("Format change prevented by active race")
        RHUI.emit_race_status()

@SOCKET_IO.on('add_race_format')
@catchLogExceptionsWrapper
def on_add_race_format(data):
    '''Adds new format in the database by duplicating an existing one.'''
    source_format_id = data['source_format_id']
    _new_format = RHData.duplicate_raceFormat(source_format_id)
    RHUI.emit_format_data()

@SOCKET_IO.on('alter_race_format')
@catchLogExceptionsWrapper
def on_alter_race_format(data):
    ''' update race format '''
    race_format, race_list = RHData.alter_raceFormat(data)

    if race_format != False:
        RACE.format = race_format
        RHUI.emit_current_laps()

        if 'format_name' in data:
            RHUI.emit_format_data()
            RHUI.emit_class_data()

        if len(race_list):
            RHUI.emit_result_data()
            message = __('Alterations made to race format: {0}').format(RACE.format.name)
            RHUI.emit_priority_message(message, False)
    else:
        RHUI.emit_priority_message(__('Format alteration prevented by active race: Stop and save/discard laps'), False, nobroadcast=True)

@SOCKET_IO.on('delete_race_format')
@catchLogExceptionsWrapper
def on_delete_race_format(data):
    '''Delete race format'''
    format_id = data['format_id']
    result = RHData.delete_raceFormat(format_id)

    if result:
        first_raceFormat = RHData.get_first_raceFormat()
        RACE.format = first_raceFormat
        RHUI.emit_current_laps()
        RHUI.emit_format_data()
    else:
        if RACE.race_status == RaceStatus.READY:
            RHUI.emit_priority_message(__('Format deletion prevented: saved race exists with this format'), False, nobroadcast=True)
        else:
            RHUI.emit_priority_message(__('Format deletion prevented by active race: Stop and save/discard laps'), False, nobroadcast=True)

# LED Effects

def emit_led_effect_setup(**_params):
    '''Emits LED event/effect wiring options.'''
    if led_manager.isEnabled():
        effects = led_manager.getRegisteredEffects()

        emit_payload = {
            'events': []
        }

        for event in LEDEvent.configurable_events:
            selectedEffect = led_manager.getEventEffect(event['event'])

            effect_list_recommended = []
            effect_list_normal = []

            for effect in effects:

                if event['event'] in effects[effect]['validEvents'].get('include', []) or (
                    event['event'] not in [Evt.SHUTDOWN, LEDEvent.IDLE_DONE, LEDEvent.IDLE_RACING, LEDEvent.IDLE_READY]
                    and event['event'] not in effects[effect]['validEvents'].get('exclude', [])
                    and Evt.ALL not in effects[effect]['validEvents'].get('exclude', [])):

                    if event['event'] in effects[effect]['validEvents'].get('recommended', []) or \
                        Evt.ALL in effects[effect]['validEvents'].get('recommended', []):
                        effect_list_recommended.append({
                            'name': effect,
                            'label': '* ' + __(effects[effect]['label'])
                        })
                    else:
                        effect_list_normal.append({
                            'name': effect,
                            'label': __(effects[effect]['label'])
                        })

            effect_list_recommended.sort(key=lambda x: x['label'])
            effect_list_normal.sort(key=lambda x: x['label'])

            emit_payload['events'].append({
                'event': event["event"],
                'label': __(event["label"]),
                'selected': selectedEffect,
                'effects': effect_list_recommended + effect_list_normal
            })

        # never broadcast
        emit('led_effect_setup_data', emit_payload)

def emit_led_effects(**_params):
    if led_manager.isEnabled() or (CLUSTER and CLUSTER.hasRecEventsSecondaries()):
        effects = led_manager.getRegisteredEffects()

        effect_list = []
        if effects:
            for effect in effects:
                if effects[effect]['validEvents'].get('manual', True):
                    effect_list.append({
                        'name': effect,
                        'label': __(effects[effect]['label'])
                    })

        emit_payload = {
            'effects': effect_list
        }

        # never broadcast
        emit('led_effects', emit_payload)

@SOCKET_IO.on('set_led_event_effect')
@catchLogExceptionsWrapper
def on_set_led_effect(data):
    '''Set effect for event.'''
    if 'event' in data and 'effect' in data:
        if led_manager.isEnabled():
            led_manager.setEventEffect(data['event'], data['effect'])

        effect_opt = RHData.get_option('ledEffects')
        if effect_opt:
            effects = json.loads(effect_opt)
        else:
            effects = {}

        effects[data['event']] = data['effect']
        RHData.set_option('ledEffects', json.dumps(effects))

        Events.trigger(Evt.LED_EFFECT_SET, {
            'effect': data['event'],
            })

        logger.info('Set LED event {0} to effect {1}'.format(data['event'], data['effect']))

@SOCKET_IO.on('use_led_effect')
@catchLogExceptionsWrapper
def on_use_led_effect(data):
    '''Activate arbitrary LED Effect.'''
    if 'effect' in data:
        if led_manager.isEnabled():
            led_manager.setEventEffect(Evt.LED_MANUAL, data['effect'])
        Events.trigger(Evt.LED_SET_MANUAL, data)  # setup manual effect on mirror timers

        args = {}
        if 'args' in data:
            args = data['args']
        if 'color' in data:
            args['color'] = hexToColor(data['color'])

        Events.trigger(Evt.LED_MANUAL, args)

# Race management socket io events

@SOCKET_IO.on('schedule_race')
@catchLogExceptionsWrapper
def on_schedule_race(data):
    RACE.scheduled_time = monotonic() + (data['m'] * 60) + data['s']
    RACE.scheduled = True

    Events.trigger(Evt.RACE_SCHEDULE, {
        'scheduled_at': RACE.scheduled_time
        })

    SOCKET_IO.emit('race_scheduled', {
        'scheduled': RACE.scheduled,
        'scheduled_at': RACE.scheduled_time
        })

    RHUI.emit_priority_message(__("Next race begins in {0:01d}:{1:02d}".format(data['m'], data['s'])), True)

@SOCKET_IO.on('cancel_schedule_race')
@catchLogExceptionsWrapper
def cancel_schedule_race():
    RACE.scheduled = False

    Events.trigger(Evt.RACE_SCHEDULE_CANCEL)

    SOCKET_IO.emit('race_scheduled', {
        'scheduled': RACE.scheduled,
        'scheduled_at': RACE.scheduled_time
        })

    RHUI.emit_priority_message(__("Scheduled race cancelled"), False)

@SOCKET_IO.on('get_pi_time')
@catchLogExceptionsWrapper
def on_get_pi_time():
    # never broadcasts to all (client must make request)
    emit('pi_time', {
        'pi_time_s': monotonic()
    })

@SOCKET_IO.on('stage_race')
@catchLogExceptionsWrapper
def on_stage_race():
    global LAST_RACE
    heat_data = RHData.get_heat(RACE.current_heat)
    race_format = RACE.format

    if heat_data:
        heatNodes = RHData.get_heatNodes_by_heat(RACE.current_heat)
        pilot_names_list = []
        for heatNode in heatNodes:
            if heatNode.node_index is not None and heatNode.node_index < RACE.num_nodes:
                if heatNode.pilot_id != RHUtils.PILOT_ID_NONE:
                    pilot_obj = RHData.get_pilot(heatNode.pilot_id)
                    if pilot_obj and pilot_obj.callsign:
                        pilot_names_list.append(pilot_obj.callsign)

        if request and len(pilot_names_list) <= 0:
            RHUI.emit_priority_message(__('No valid pilots in race'), True, nobroadcast=True)

        logger.info("Staging new race, format: {}".format(getattr(race_format, "name", "????")))
        max_round = RHData.get_max_round(RACE.current_heat)
        if max_round is None:
            max_round = 0
        logger.info("Racing heat '{}' round {}, pilots: {}".format(heat_data.displayname(), (max_round+1),
                                                                   ", ".join(pilot_names_list)))
    else:
        heatNodes = []

        profile_freqs = json.loads(RACE.profile.frequencies)

        class FauxHeatNode():
            node_index = None
            pilot_id = 1

        for idx in range(RACE.num_nodes):
            if (profile_freqs["f"][idx]):
                heatNode = FauxHeatNode
                heatNode.node_index = idx
                heatNodes.append(heatNode)

    if CLUSTER:
        CLUSTER.emitToSplits('stage_race')

    if RACE.race_status != RaceStatus.READY:
        if race_format is SECONDARY_RACE_FORMAT:  # if running as secondary timer
            if RACE.race_status == RaceStatus.RACING:
                return  # if race in progress then leave it be
            # if missed stop/discard message then clear current race
            logger.info("Forcing race clear/restart because running as secondary timer")
            on_discard_laps()
        elif RACE.race_status == RaceStatus.DONE and not RACE.any_laps_recorded():
            on_discard_laps()  # if no laps then allow restart

    if RACE.race_status == RaceStatus.READY: # only initiate staging if ready
        # common race start events (do early to prevent processing delay when start is called)
        INTERFACE.enable_calibration_mode() # Nodes reset triggers on next pass

        if heat_data and heat_data.class_id != RHUtils.CLASS_ID_NONE:
            class_format_id = RHData.get_raceClass(heat_data.class_id).format_id
            if class_format_id != RHUtils.FORMAT_ID_NONE:
                RACE.format = RHData.get_raceFormat(class_format_id)
                RHUI.emit_current_laps()
                logger.info("Forcing race format from class setting: '{0}' ({1})".format(RACE.format.name, RACE.format.id))

        clear_laps() # Clear laps before race start
        init_node_cross_fields()  # set 'cur_pilot_id' and 'cross' fields on nodes
        LAST_RACE = None # clear all previous race data
        RACE.timer_running = False # indicate race timer not running
        RACE.race_status = RaceStatus.STAGING
        RACE.win_status = WinStatus.NONE
        RACE.status_message = ''
        RACE.any_races_started = True

        RACE.init_node_finished_flags(heatNodes)

        INTERFACE.set_race_status(RaceStatus.STAGING)
        RHUI.emit_current_laps() # Race page, blank laps to the web client
        RHUI.emit_current_leaderboard() # Race page, blank leaderboard to the web client
        RHUI.emit_race_status()

        staging_fixed_ms = (0 if race_format.staging_fixed_tones <= 1 else race_format.staging_fixed_tones - 1) * 1000

        staging_random_ms = random.randint(0, race_format.start_delay_max_ms)
        hide_stage_timer = (race_format.start_delay_max_ms > 0)

        staging_total_ms = staging_fixed_ms + race_format.start_delay_min_ms + staging_random_ms

        if race_format.staging_tones == StagingTones.TONES_NONE:
            if staging_total_ms > 0:
                staging_tones = race_format.staging_fixed_tones
            else:
                staging_tones = staging_fixed_ms / 1000
        else:
            staging_tones = staging_total_ms // 1000
            if staging_random_ms % 1000:
                staging_tones += 1

        RACE.stage_time_monotonic = monotonic() + float(Config.GENERAL['RACE_START_DELAY_EXTRA_SECS'])
        RACE.start_time_monotonic = RACE.stage_time_monotonic + (staging_total_ms / 1000 )

        RACE.start_time_epoch_ms = monotonic_to_epoch_millis(RACE.start_time_monotonic)
        RACE.start_token = random.random()
        gevent.spawn(race_start_thread, RACE.start_token)

        eventPayload = {
            'hide_stage_timer': hide_stage_timer,
            'pi_staging_at_s': RACE.stage_time_monotonic,
            'staging_tones': staging_tones,
            'pi_starts_at_s': RACE.start_time_monotonic,
            'color': ColorVal.ORANGE,
        }

        if led_manager.isEnabled():
            eventPayload['race_node_colors'] = led_manager.getNodeColors(RACE.num_nodes)
        else:
            eventPayload['race_node_colors'] = None

        Events.trigger(Evt.RACE_STAGE, eventPayload)

        SOCKET_IO.emit('stage_ready', {
            'hide_stage_timer': hide_stage_timer,
            'pi_staging_at_s': RACE.stage_time_monotonic,
            'staging_tones': staging_tones,
            'pi_starts_at_s': RACE.start_time_monotonic,
            'race_mode': race_format.race_mode,
            'race_time_sec': race_format.race_time_sec,
        }) # Announce staging with final parameters

    else:
        logger.info("Attempted to stage race while status is not 'ready'")

def autoUpdateCalibration():
    ''' Apply best tuning values to nodes '''
    if RACE.current_heat == RHUtils.HEAT_ID_NONE:
        logger.debug('Skipping auto calibration; server in practice mode')
        return None

    for node_index, node in enumerate(INTERFACE.nodes):
        calibration = findBestValues(node, node_index)

        if node.enter_at_level is not calibration['enter_at_level']:
            on_set_enter_at_level({
                'node': node_index,
                'enter_at_level': calibration['enter_at_level']
            })

        if node.exit_at_level is not calibration['exit_at_level']:
            on_set_exit_at_level({
                'node': node_index,
                'exit_at_level': calibration['exit_at_level']
            })

    logger.info('Updated calibration with best discovered values')
    RHUI.emit_enter_and_exit_at_levels()

def findBestValues(node, node_index):
    ''' Search race history for best tuning values '''

    # get commonly used values
    heat = RHData.get_heat(RACE.current_heat)
    pilot = RHData.get_pilot_from_heatNode(RACE.current_heat, node_index)
    current_class = heat.class_id
    races = RHData.get_savedRaceMetas()
    races.sort(key=lambda x: x.id, reverse=True)
    pilotRaces = RHData.get_savedPilotRaces()
    pilotRaces.sort(key=lambda x: x.id, reverse=True)

    # test for disabled node
    if pilot is RHUtils.PILOT_ID_NONE or node.frequency is RHUtils.FREQUENCY_ID_NONE:
        logger.debug('Node {0} calibration: skipping disabled node'.format(node.index+1))
        return {
            'enter_at_level': node.enter_at_level,
            'exit_at_level': node.exit_at_level
        }

    # test for same heat, same node
    for race in races:
        if race.heat_id == heat.id:
            for pilotRace in pilotRaces:
                if pilotRace.race_id == race.id and \
                    pilotRace.node_index == node_index:
                    logger.debug('Node {0} calibration: found same pilot+node in same heat'.format(node.index+1))
                    return {
                        'enter_at_level': pilotRace.enter_at,
                        'exit_at_level': pilotRace.exit_at
                    }
            break

    # test for same class, same pilot, same node
    for race in races:
        if race.class_id == current_class:
            for pilotRace in pilotRaces:
                if pilotRace.race_id == race.id and \
                    pilotRace.node_index == node_index and \
                    pilotRace.pilot_id == pilot:
                    logger.debug('Node {0} calibration: found same pilot+node in other heat with same class'.format(node.index+1))
                    return {
                        'enter_at_level': pilotRace.enter_at,
                        'exit_at_level': pilotRace.exit_at
                    }
            break

    # test for same pilot, same node
    for pilotRace in pilotRaces:
        if pilotRace.node_index == node_index and \
            pilotRace.pilot_id == pilot:
            logger.debug('Node {0} calibration: found same pilot+node in other heat with other class'.format(node.index+1))
            return {
                'enter_at_level': pilotRace.enter_at,
                'exit_at_level': pilotRace.exit_at
            }

    # test for same node
    for pilotRace in pilotRaces:
        if pilotRace.node_index == node_index:
            logger.debug('Node {0} calibration: found same node in other heat'.format(node.index+1))
            return {
                'enter_at_level': pilotRace.enter_at,
                'exit_at_level': pilotRace.exit_at
            }

    # fallback
    logger.debug('Node {0} calibration: no calibration hints found, no change'.format(node.index+1))
    return {
        'enter_at_level': node.enter_at_level,
        'exit_at_level': node.exit_at_level
    }

@catchLogExceptionsWrapper
def race_start_thread(start_token):

    # clear any lingering crossings at staging (if node rssi < enterAt)
    for node in INTERFACE.nodes:
        if node.crossing_flag and node.frequency > 0 and \
            (RACE.format is SECONDARY_RACE_FORMAT or
            (node.current_pilot_id != RHUtils.PILOT_ID_NONE and node.current_rssi < node.enter_at_level)):
            logger.info("Forcing end crossing for node {0} at staging (rssi={1}, enterAt={2}, exitAt={3})".\
                       format(node.index+1, node.current_rssi, node.enter_at_level, node.exit_at_level))
            INTERFACE.force_end_crossing(node.index)

    if CLUSTER and CLUSTER.hasSecondaries():
        CLUSTER.doClusterRaceStart()

    # set lower EnterAt/ExitAt values if configured
    if RHData.get_optionInt('startThreshLowerAmount') > 0 and RHData.get_optionInt('startThreshLowerDuration') > 0:
        lower_amount = RHData.get_optionInt('startThreshLowerAmount')
        logger.info("Lowering EnterAt/ExitAt values at start of race, amount={0}%, duration={1} secs".\
                    format(lower_amount, RHData.get_optionInt('startThreshLowerDuration')))
        lower_end_time = RACE.start_time_monotonic + RHData.get_optionInt('startThreshLowerDuration')
        for node in INTERFACE.nodes:
            if node.frequency > 0 and (RACE.format is SECONDARY_RACE_FORMAT or node.current_pilot_id != RHUtils.PILOT_ID_NONE):
                if node.current_rssi < node.enter_at_level:
                    diff_val = int((node.enter_at_level-node.exit_at_level)*lower_amount/100)
                    if diff_val > 0:
                        new_enter_at = node.enter_at_level - diff_val
                        new_exit_at = max(node.exit_at_level - diff_val, 0)
                        if node.api_valid_flag and node.is_valid_rssi(new_enter_at):
                            logger.info("For node {0} lowering EnterAt from {1} to {2} and ExitAt from {3} to {4}"\
                                    .format(node.index+1, node.enter_at_level, new_enter_at, node.exit_at_level, new_exit_at))
                            node.start_thresh_lower_time = lower_end_time  # set time when values will be restored
                            node.start_thresh_lower_flag = True
                            # use 'transmit_' instead of 'set_' so values are not saved in node object
                            INTERFACE.transmit_enter_at_level(node, new_enter_at)
                            INTERFACE.transmit_exit_at_level(node, new_exit_at)
                    else:
                        logger.info("Not lowering EnterAt/ExitAt values for node {0} because EnterAt value ({1}) unchanged"\
                                .format(node.index+1, node.enter_at_level))
                else:
                    logger.info("Not lowering EnterAt/ExitAt values for node {0} because current RSSI ({1}) >= EnterAt ({2})"\
                            .format(node.index+1, node.current_rssi, node.enter_at_level))

    # do non-blocking delay before time-critical code
    while (monotonic() < RACE.start_time_monotonic - 0.5):
        gevent.sleep(0.1)

    if RACE.race_status == RaceStatus.STAGING and \
        RACE.start_token == start_token:
        # Only start a race if it is not already in progress
        # Null this thread if token has changed (race stopped/started quickly)

        # do blocking delay until race start
        while monotonic() < RACE.start_time_monotonic:
            pass

        # !!! RACE STARTS NOW !!!

        # do time-critical tasks
        Events.trigger(Evt.RACE_START, {
            'race': RACE,
            'color': ColorVal.GREEN
            })

        # do secondary start tasks (small delay is acceptable)
        RACE.start_time = datetime.now() # record standard-formatted time

        for node in INTERFACE.nodes:
            node.history_values = [] # clear race history
            node.history_times = []
            node.under_min_lap_count = 0
            # clear any lingering crossing (if rssi>enterAt then first crossing starts now)
            if node.crossing_flag and node.frequency > 0 and (
                RACE.format is SECONDARY_RACE_FORMAT or node.current_pilot_id != RHUtils.PILOT_ID_NONE):
                logger.info("Forcing end crossing for node {0} at start (rssi={1}, enterAt={2}, exitAt={3})".\
                           format(node.index+1, node.current_rssi, node.enter_at_level, node.exit_at_level))
                INTERFACE.force_end_crossing(node.index)

        RACE.race_status = RaceStatus.RACING # To enable registering passed laps
        INTERFACE.set_race_status(RaceStatus.RACING)
        RACE.timer_running = True # indicate race timer is running
        RACE.laps_winner_name = None  # name of winner in first-to-X-laps race
        RACE.winning_lap_id = 0  # track winning lap-id if race tied during first-to-X-laps race

        # kick off race expire processing
        race_format = RACE.format
        if race_format and race_format.race_mode == 0: # count down
            gevent.spawn(race_expire_thread, start_token)

        RHUI.emit_race_status() # Race page, to set race button states
        logger.info('Race started at {:.3f} ({:.0f})'.format(RACE.start_time_monotonic, RACE.start_time_epoch_ms))

@catchLogExceptionsWrapper
def race_expire_thread(start_token):
    race_format = RACE.format
    if race_format and race_format.race_mode == 0: # count down
        gevent.sleep(race_format.race_time_sec)
        # if race still in progress and is still same race
        if RACE.race_status == RaceStatus.RACING and RACE.start_token == start_token:
            logger.info("Race count-down timer reached expiration")
            RACE.timer_running = False # indicate race timer no longer running
            Events.trigger(Evt.RACE_FINISH)
            PassInvokeFuncQueueObj.waitForQueueEmpty()  # wait until any active pass-record processing is finished
            check_win_condition(at_finish=True, start_token=start_token)
            RHUI.emit_current_leaderboard()
            if race_format.lap_grace_sec > -1:
                gevent.sleep((RACE.start_time_monotonic + race_format.race_time_sec + race_format.lap_grace_sec) - monotonic())
                if RACE.race_status == RaceStatus.RACING and RACE.start_token == start_token:
                    on_stop_race()
                    logger.debug("Race grace period reached")
                else:
                    logger.debug("Grace period timer {} is unused".format(start_token))
        else:
            logger.debug("Race-time-expire thread {} is unused".format(start_token))

@SOCKET_IO.on('stop_race')
@catchLogExceptionsWrapper
def on_stop_race(doSave=False):
    '''Stops the race and stops registering laps.'''
    if CLUSTER:
        CLUSTER.emitToSplits('stop_race')

    if RACE.race_status == RaceStatus.RACING:
        # clear any crossings still in progress
        any_forced_flag = False
        for node in INTERFACE.nodes:
            if node.crossing_flag and node.frequency > 0 and \
                            node.current_pilot_id != RHUtils.PILOT_ID_NONE:
                logger.info("Forcing end crossing for node {} at race stop (rssi={}, enterAt={}, exitAt={})".\
                            format(node.index+1, node.current_rssi, node.enter_at_level, node.exit_at_level))
                INTERFACE.force_end_crossing(node.index)
                any_forced_flag = True
        if any_forced_flag:  # give forced end-crossings a chance to complete before stopping race
            gevent.spawn_later(0.5, do_stop_race_actions, doSave)
        else:
            do_stop_race_actions(doSave)
    else:
        do_stop_race_actions(doSave)

    SOCKET_IO.emit('stop_timer') # Loop back to race page to stop the timer

@catchLogExceptionsWrapper
def do_stop_race_actions(doSave=False):
    if RACE.race_status == RaceStatus.RACING:
        RACE.end_time = monotonic() # Update the race end time stamp
        delta_time = RACE.end_time - RACE.start_time_monotonic

        logger.info('Race stopped at {:.3f} ({:.0f}), duration {:.0f}s'.format(RACE.end_time, monotonic_to_epoch_millis(RACE.end_time), delta_time))

        min_laps_list = []  # show nodes with laps under minimum (if any)
        for node in INTERFACE.nodes:
            if node.under_min_lap_count > 0:
                min_laps_list.append('Node {0} Count={1}'.format(node.index+1, node.under_min_lap_count))
        if len(min_laps_list) > 0:
            logger.info('Nodes with laps under minimum:  ' + ', '.join(min_laps_list))

        RACE.race_status = RaceStatus.DONE # To stop registering passed laps, waiting for laps to be cleared
        INTERFACE.set_race_status(RaceStatus.DONE)

        Events.trigger(Evt.RACE_STOP, {
            'color': ColorVal.RED
        })
        PassInvokeFuncQueueObj.waitForQueueEmpty()  # wait until any active pass-record processing is finished
        check_win_condition()

        if CLUSTER and CLUSTER.hasSecondaries():
            CLUSTER.doClusterRaceStop()

    elif RACE.race_status == RaceStatus.STAGING:
        logger.info('Stopping race during staging')
        RACE.race_status = RaceStatus.READY # Go back to ready state
        INTERFACE.set_race_status(RaceStatus.READY)
        Events.trigger(Evt.LAPS_CLEAR)
        delta_time = 0

    else:
        RACE.race_status = RaceStatus.DONE # To stop registering passed laps, waiting for laps to be cleared
        INTERFACE.set_race_status(RaceStatus.DONE)

        logger.debug('No active race to stop')
        delta_time = 0

    # check if nodes may be set to temporary lower EnterAt/ExitAt values (and still have them)
    if RHData.get_optionInt('startThreshLowerAmount') > 0 and \
            delta_time < RHData.get_optionInt('startThreshLowerDuration'):
        for node in INTERFACE.nodes:
            # if node EnterAt/ExitAt values need to be restored then do it soon
            if node.frequency > 0 and (
                RACE.format is SECONDARY_RACE_FORMAT or (
                    node.current_pilot_id != RHUtils.PILOT_ID_NONE and \
                    node.start_thresh_lower_flag)):
                node.start_thresh_lower_time = RACE.end_time + 0.1

    RACE.timer_running = False # indicate race timer not running
    RACE.scheduled = False # also stop any deferred start

    RHUI.emit_race_status() # Race page, to set race button states
    RHUI.emit_current_leaderboard()

    if doSave:
        do_save_actions()

@SOCKET_IO.on('save_laps')
@catchLogExceptionsWrapper
def on_save_laps(_data=None):
    '''Handle "save" UI action'''

    if RACE.race_status == RaceStatus.RACING:
        on_stop_race(doSave=True)
    else:
        do_save_actions()

@catchLogExceptionsWrapper
def do_save_actions():
    '''Save current laps data to the database.'''
    if RACE.current_heat == RHUtils.HEAT_ID_NONE:
        on_discard_laps(saved=True)
        return False

    if CLUSTER:
        CLUSTER.emitToSplits('save_laps')

    heat = RHData.get_heat(RACE.current_heat)

    # Clear caches
    RHData.clear_results_heat(RACE.current_heat)
    RHData.clear_results_raceClass(heat.class_id)
    RHData.clear_results_event()

    # Get the last saved round for the current heat
    max_round = RHData.get_max_round(RACE.current_heat)

    if max_round is None:
        max_round = 0
    # Loop through laps to copy to saved races
    profile = RACE.profile
    profile_freqs = json.loads(profile.frequencies)

    new_race_data = {
        'round_id': max_round+1,
        'heat_id': RACE.current_heat,
        'class_id': heat.class_id,
        'format_id': RACE.format.id if hasattr(RACE.format, 'id') else RHUtils.FORMAT_ID_NONE,
        'start_time': RACE.start_time_monotonic,
        'start_time_formatted': RACE.start_time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    new_race = RHData.add_savedRaceMeta(new_race_data)

    race_data = {}

    for node_index in range(RACE.num_nodes):
        if profile_freqs["f"][node_index] != RHUtils.FREQUENCY_ID_NONE:
            pilot_id = RHData.get_pilot_from_heatNode(RACE.current_heat, node_index)

            if pilot_id is not None:
                race_data[node_index] = {
                    'race_id': new_race.id,
                    'pilot_id': pilot_id,
                    'history_values': json.dumps(INTERFACE.nodes[node_index].history_values),
                    'history_times': json.dumps(INTERFACE.nodes[node_index].history_times),
                    'enter_at': INTERFACE.nodes[node_index].enter_at_level,
                    'exit_at': INTERFACE.nodes[node_index].exit_at_level,
                    'laps': RACE.node_laps[node_index]
                    }

                RHData.set_pilot_used_frequency(pilot_id, {
                    'b': profile_freqs["b"][node_index],
                    'c': profile_freqs["c"][node_index],
                    'f': profile_freqs["f"][node_index]
                    })

    RHData.add_race_data(race_data)

    Events.trigger(Evt.LAPS_SAVE, {
        'race_id': new_race.id,
        })

    logger.info('Current laps saved: Heat {0} Round {1}'.format(RACE.current_heat, max_round+1))

    on_discard_laps(saved=True) # Also clear the current laps

    next_heat = RHData.get_next_heat_id(heat)
    if next_heat is not heat.id:
        on_set_current_heat({'heat': next_heat})

    # spawn thread for updating results caches
    cache_params = {
        'race_id': new_race.id,
        'heat_id': new_race.heat_id,
        'round_id': new_race.round_id,
    }
    gevent.spawn(build_atomic_result_caches, cache_params)

@SOCKET_IO.on('resave_laps')
@catchLogExceptionsWrapper
def on_resave_laps(data):
    heat_id = data['heat_id']
    round_id = data['round_id']
    callsign = data['callsign']

    race_id = data['race_id']
    pilotrace_id = data['pilotrace_id']
    node = data['node']
    pilot_id = data['pilot_id']
    laps = data['laps']
    enter_at = data['enter_at']
    exit_at = data['exit_at']

    pilotrace_data = {
        'pilotrace_id': pilotrace_id,
        'enter_at': enter_at,
        'exit_at': exit_at
        }

    # Clear caches
    RHData.clear_results_heat(heat_id)
    heat = RHData.get_heat(heat_id)
    RHData.clear_results_raceClass(heat.class_id)
    RHData.clear_results_savedRaceMeta(race_id)

    RHData.alter_savedPilotRace(pilotrace_data)

    new_racedata = {
            'race_id': race_id,
            'pilotrace_id': pilotrace_id,
            'node_index': node,
            'pilot_id': pilot_id,
            'laps': []
        }

    for lap in laps:
        tmp_lap_time_formatted = lap['lap_time']
        if isinstance(lap['lap_time'], float):
            tmp_lap_time_formatted = RHUtils.time_format(lap['lap_time'], RHData.get_option('timeFormat'))

        new_racedata['laps'].append({
            'lap_time_stamp': lap['lap_time_stamp'],
            'lap_time': lap['lap_time'],
            'lap_time_formatted': tmp_lap_time_formatted,
            'source': lap['source'],
            'deleted': lap['deleted']
            })

    RHData.replace_savedRaceLaps(new_racedata)

    message = __('Race times adjusted for: Heat {0} Round {1} / {2}').format(heat_id, round_id, callsign)
    RHUI.emit_priority_message(message, False)
    logger.info(message)

    # run adaptive calibration
    if RHData.get_optionInt('calibrationMode'):
        autoUpdateCalibration()

    # spawn thread for updating results caches
    params = {
        'race_id': race_id,
        'heat_id': heat_id,
        'round_id': round_id,
    }
    gevent.spawn(build_atomic_result_caches, params)

    Events.trigger(Evt.LAPS_RESAVE, {
        'race_id': race_id,
        'pilot_id': pilot_id,
        })

@catchLogExceptionsWrapper
def build_atomic_result_caches(params):
    PageCache.set_valid(False)
    Results.build_atomic_results_caches(RHData, params)
    RHUI.emit_result_data()

@SOCKET_IO.on('discard_laps')
@catchLogExceptionsWrapper
def on_discard_laps(**kwargs):
    '''Clear the current laps without saving.'''

    if RACE.race_status == RaceStatus.STAGING or RACE.race_status == RaceStatus.RACING:
        on_stop_race()

    clear_laps()
    RACE.race_status = RaceStatus.READY # Flag status as ready to start next race
    INTERFACE.set_race_status(RaceStatus.READY)
    RACE.win_status = WinStatus.NONE
    RACE.status_message = ''
    RHUI.emit_current_laps() # Race page, blank laps to the web client
    RHUI.emit_current_leaderboard() # Race page, blank leaderboard to the web client
    RHUI.emit_race_status() # Race page, to set race button states

    if 'saved' in kwargs and kwargs['saved'] == True:
        # discarding follows a save action
        pass
    else:
        # discarding does not follow a save action
        Events.trigger(Evt.LAPS_DISCARD)
        if CLUSTER:
            CLUSTER.emitToSplits('discard_laps')

    Events.trigger(Evt.LAPS_CLEAR)

def clear_laps():
    '''Clear the current laps table.'''
    branch_race_obj()
    RACE.laps_winner_name = None  # clear winner in first-to-X-laps race
    RACE.winning_lap_id = 0
    reset_current_laps() # Clear out the current laps table
    RHData.clear_lapSplits()
    logger.info('Current laps cleared')

def branch_race_obj():
    global LAST_RACE
    LAST_RACE = RHRace.RHRace()

    LAST_RACE._rhdata = RHData

    LAST_RACE.num_nodes = RACE.num_nodes
    LAST_RACE.current_heat = RACE.current_heat
    LAST_RACE.node_pilots = RACE.node_pilots
    LAST_RACE.node_teams = RACE.node_teams
    LAST_RACE.format = RACE.format
    LAST_RACE.profile = RACE.profile 
    # sequence
    LAST_RACE.scheduled = RACE.scheduled
    LAST_RACE.scheduled_time = RACE.scheduled_time
    LAST_RACE.start_token = RACE.start_token
    # status
    LAST_RACE.race_status = RACE.race_status
    LAST_RACE.timer_running = RACE.timer_running
    LAST_RACE.stage_time_monotonic = RACE.stage_time_monotonic
    LAST_RACE.start_time = RACE.start_time
    LAST_RACE.start_time_monotonic = RACE.start_time_monotonic
    LAST_RACE.start_time_epoch_ms = RACE.start_time_epoch_ms
    LAST_RACE.node_laps = RACE.node_laps 
    LAST_RACE.node_has_finished = RACE.node_has_finished 
    LAST_RACE.any_races_started = RACE.any_races_started
    # concluded
    LAST_RACE.end_time = RACE.end_time
    # leaderboard/cache
    LAST_RACE.results = RACE.results
    LAST_RACE.cacheStatus = RACE.cacheStatus
    LAST_RACE.status_message = RACE.status_message

    LAST_RACE.team_results = RACE.team_results
    LAST_RACE.team_cacheStatus = RACE.team_cacheStatus
    LAST_RACE.win_status = RACE.win_status

    led_manager.LAST_RACE = LAST_RACE
    RHUI._last_race = LAST_RACE

def init_node_cross_fields():
    '''Sets the 'current_pilot_id' and 'cross' values on each node.'''
    for node in INTERFACE.nodes:
        node.current_pilot_id = RHUtils.PILOT_ID_NONE
        if node.frequency and node.frequency > 0:
            if RACE.current_heat is not RHUtils.HEAT_ID_NONE:
                heatnodes = RHData.get_heatNodes_by_heat(RACE.current_heat)
                for heatnode in heatnodes:
                    if heatnode.node_index == node.index:
                        node.current_pilot_id = heatnode.pilot_id
                        break

        node.first_cross_flag = False
        node.show_crossing_flag = False

@SOCKET_IO.on('calc_pilots')
@catchLogExceptionsWrapper
def on_calc_pilots(data):
    heat_id = data['heat']
    calc_heat(heat_id)

@SOCKET_IO.on('calc_reset')
@catchLogExceptionsWrapper
def on_calc_reset(data):
    data['status'] = Database.HeatStatus.PLANNED
    on_alter_heat(data)
    RHUI.emit_heat_data()

def calc_heat(heat_id, silent=False):
    heat = RHData.get_heat(heat_id)

    if (heat):
        calc_result = RHData.calc_heat_pilots(heat_id, Results)

        if calc_result['calc_success'] is False:
            logger.warning('{} plan cannot be fulfilled.'.format(heat.displayname()))

        if calc_result['calc_success'] is None:
            # Heat is confirmed or has saved races
            return 'safe'

        if calc_result['calc_success'] is True and calc_result['has_calc_pilots'] is False and not heat.auto_frequency:
            # Heat has no calc issues, no dynamic slots, and auto-frequnecy is off
            return 'safe'

        adaptive = bool(RHData.get_optionInt('calibrationMode'))

        if adaptive:
            calc_fn = RHUtils.find_best_slot_node_adaptive
        else:
            calc_fn = RHUtils.find_best_slot_node_basic

        RHData.run_auto_frequency(heat_id, RACE.profile.frequencies, RACE.num_nodes, calc_fn)

        if request and not silent:
            emit_heat_plan_result(heat_id, calc_result)

        return 'unsafe'

    else:
        return 'no-heat'

def set_current_heat_data(new_heat_id, silent=False):
    result = calc_heat(new_heat_id, silent)

    if result == 'safe':
        finalize_current_heat_set(new_heat_id)
    elif result == 'no-heat':
        finalize_current_heat_set(RHUtils.HEAT_ID_NONE)

def emit_heat_plan_result(new_heat_id, calc_result):
    heat = RHData.get_heat(new_heat_id)
    heatNodes = []

    heatNode_objs = RHData.get_heatNodes_by_heat(heat.id)
    heatNode_objs.sort(key=lambda x: x.id)

    profile_freqs = json.loads(RACE.profile.frequencies)

    for heatNode in heatNode_objs:
        heatNode_data = {
            'node_index': heatNode.node_index,
            'pilot_id': heatNode.pilot_id,
            'callsign': None,
            'method': heatNode.method,
            'seed_rank': heatNode.seed_rank,
            'seed_id': heatNode.seed_id
            }
        if heatNode.pilot_id:
            pilot = RHData.get_pilot(heatNode.pilot_id)
            if pilot:
                heatNode_data['callsign'] = pilot.callsign
                if pilot.used_frequencies:
                    used_freqs = json.loads(pilot.used_frequencies)
                    heatNode_data['frequency_change'] = (used_freqs[-1]['f'] != profile_freqs["f"][heatNode.node_index])
                else:
                    heatNode_data['frequency_change'] = True

        heatNodes.append(heatNode_data)

    emit_payload = {
        'heat': new_heat_id,
        'displayname': heat.displayname(),
        'slots': heatNodes,
        'calc_result': calc_result
    }

    emit('heat_plan_result', emit_payload)

@SOCKET_IO.on('confirm_heat_plan')
@catchLogExceptionsWrapper
def on_confirm_heat(data):
    if 'heat_id' in data:
        RHData.alter_heat({
            'heat': data['heat_id'],
            'status': Database.HeatStatus.CONFIRMED
            }
        )
        RHData.resolve_slot_unset_nodes(data['heat_id'])
        RHUI.emit_heat_data()
        finalize_current_heat_set(data['heat_id'])

def finalize_current_heat_set(new_heat_id):
    RACE.current_heat = new_heat_id

    if new_heat_id == RHUtils.HEAT_ID_NONE:
        RACE.node_pilots = {}
        RACE.node_teams = {}
        logger.info("Switching to practice mode; races will not be saved until a heat is selected")

    else:
        RACE.node_pilots = {}
        RACE.node_teams = {}
        for idx in range(RACE.num_nodes):
            RACE.node_pilots[idx] = RHUtils.PILOT_ID_NONE
            RACE.node_teams[idx] = None

        for heatNode in RHData.get_heatNodes_by_heat(new_heat_id):
            if heatNode.node_index is not None:
                RACE.node_pilots[heatNode.node_index] = heatNode.pilot_id

                if heatNode.pilot_id is not RHUtils.PILOT_ID_NONE:
                    RACE.node_teams[heatNode.node_index] = RHData.get_pilot(heatNode.pilot_id).team
                else:
                    RACE.node_teams[heatNode.node_index] = None

        heat_data = RHData.get_heat(new_heat_id)

        if heat_data.class_id != RHUtils.CLASS_ID_NONE:
            class_format_id = RHData.get_raceClass(heat_data.class_id).format_id
            if class_format_id != RHUtils.FORMAT_ID_NONE:
                RACE.format = RHData.get_raceFormat(class_format_id)
                RHUI.emit_current_laps()
                logger.info("Forcing race format from class setting: '{0}' ({1})".format(RACE.format.name, RACE.format.id))

        adaptive = bool(RHData.get_optionInt('calibrationMode'))
        if adaptive:
            autoUpdateCalibration()

    Events.trigger(Evt.HEAT_SET, {
        'heat_id': new_heat_id,
        })

    RACE.clear_results() # refresh leaderboard

    RHUI.emit_current_heat() # Race page, to update heat selection button
    RHUI.emit_current_leaderboard() # Race page, to update callsigns in leaderboard
    RHUI.emit_race_status()

@SOCKET_IO.on('set_current_heat')
@catchLogExceptionsWrapper
def on_set_current_heat(data):
    '''Update the current heat variable and data.'''
    new_heat_id = data['heat']
    logger.info('Setting current heat to Heat {0}'.format(new_heat_id))
    set_current_heat_data(new_heat_id)

@SOCKET_IO.on('delete_lap')
@catchLogExceptionsWrapper
def on_delete_lap(data):
    '''Delete a false lap.'''

    node_index = data['node']
    lap_index = data['lap_index']

    if node_index is None or lap_index is None:
        logger.error("Bad parameter in 'on_delete_lap()':  node_index={0}, lap_index={1}".format(node_index, lap_index))
        return

    RACE.node_laps[node_index][lap_index]['invalid'] = True

    time = RACE.node_laps[node_index][lap_index]['lap_time_stamp']

    race_format = RACE.format
    RACE.set_node_finished_flag(node_index, False)
    lap_number = 0
    for lap in RACE.node_laps[node_index]:
        lap['deleted'] = False
        if RACE.get_node_finished_flag(node_index):
            lap['late_lap'] = True
            lap['deleted'] = True
        else:
            lap['late_lap'] = False

        if lap.get('invalid', False):
            lap['lap_number'] = None
            lap['deleted'] = True
        else:
            lap['lap_number'] = lap_number
            if race_format.race_mode == 0 and lap['lap_time_stamp'] > (race_format.race_time_sec * 1000) or \
                (race_format.win_condition == WinCondition.FIRST_TO_LAP_X and lap_number >= race_format.number_laps_win):
                RACE.set_node_finished_flag(node_index)
            lap_number += 1

    db_last = False
    db_next = False
    for lap in RACE.node_laps[node_index]:
        if not lap.get('invalid', False) and ((not lap['deleted']) or lap.get('late_lap', False)):
            if lap['lap_time_stamp'] < time:
                db_last = lap
            if lap['lap_time_stamp'] > time:
                db_next = lap
                break

    if db_next and db_last:
        db_next['lap_time'] = db_next['lap_time_stamp'] - db_last['lap_time_stamp']
        db_next['lap_time_formatted'] = RHUtils.time_format(db_next['lap_time'], RHData.get_option('timeFormat'))
    elif db_next:
        db_next['lap_time'] = db_next['lap_time_stamp']
        db_next['lap_time_formatted'] = RHUtils.time_format(db_next['lap_time'], RHData.get_option('timeFormat'))

    try:  # delete any split laps for deleted lap
        lap_splits = RHData.get_lapSplits_by_lap(node_index, lap_number)
        if lap_splits and len(lap_splits) > 0:
            for lap_split in lap_splits:
                RHData.clear_lapSplit(lap_split)
    except:
        logger.exception("Error deleting split laps")

    Events.trigger(Evt.LAP_DELETE, {
        #'race': RACE,  # TODO this causes exceptions via 'json.loads()', so leave out for now
        'node_index': node_index,
        })

    logger.info('Lap deleted: Node {0} LapIndex {1}'.format(node_index+1, lap_index))

    RACE.clear_results()
    PassInvokeFuncQueueObj.waitForQueueEmpty()  # wait until any active pass-record processing is finished
    check_win_condition(deletedLap=True)  # handle possible change in win status

    RHUI.emit_current_laps() # Race page, update web client
    RHUI.emit_current_leaderboard() # Race page, update web client

@SOCKET_IO.on('restore_deleted_lap')
@catchLogExceptionsWrapper
def on_restore_deleted_lap(data):
    '''Restore a deleted (or "late") lap.'''

    node_index = data['node']
    lap_index = data['lap_index']

    if node_index is None or lap_index is None:
        logger.error("Bad parameter in 'on_restore_deleted_lap()':  node_index={0}, lap_index={1}".format(node_index, lap_index))
        return

    lap_obj = RACE.node_laps[node_index][lap_index]

    lap_obj['deleted'] = False
    lap_obj['late_lap'] = False

    lap_number = 0  # adjust lap numbers and times as needed
    last_lap_ts = 0
    for idx, lap in enumerate(RACE.node_laps[node_index]):
        if not lap['deleted']:
            if idx >= lap_index:
                lap['lap_number'] = lap_number
                lap['lap_time'] = lap['lap_time_stamp'] - last_lap_ts
                lap['lap_time_formatted'] = RHUtils.time_format(lap['lap_time'], RHData.get_option('timeFormat'))
            last_lap_ts = lap['lap_time_stamp']
            lap_number += 1

    Events.trigger(Evt.LAP_RESTORE_DELETED, {
        #'race': RACE,  # TODO this causes exceptions via 'json.loads()', so leave out for now
        'node_index': node_index,
        })

    logger.info('Restored deleted lap: Node {0} LapIndex {1}'.format(node_index+1, lap_index))

    RACE.clear_results()
    PassInvokeFuncQueueObj.waitForQueueEmpty()  # wait until any active pass-record processing is finished
    check_win_condition(deletedLap=True)  # handle possible change in win status

    RHUI.emit_current_laps() # Race page, update web client
    RHUI.emit_current_leaderboard() # Race page, update web client

@SOCKET_IO.on('simulate_lap')
@catchLogExceptionsWrapper
def on_simulate_lap(data):
    '''Simulates a lap (for debug testing).'''
    node_index = data['node']
    logger.info('Simulated lap: Node {0}'.format(node_index+1))
    Events.trigger(Evt.CROSSING_EXIT, {
        'nodeIndex': node_index,
        'color': led_manager.getDisplayColor(node_index)
        })
    INTERFACE.intf_simulate_lap(node_index, 0)

@SOCKET_IO.on('LED_solid')
@catchLogExceptionsWrapper
def on_LED_solid(data):
    '''LED Solid Color'''
    if 'off' in data and data['off']:
        led_manager.clear()
    else:
        led_red = data['red']
        led_green = data['green']
        led_blue = data['blue']

        on_use_led_effect({
            'effect': "stripColor",
            'args': {
                'color': Color(led_red,led_green,led_blue),
                'pattern': ColorPattern.SOLID,
                'preventIdle': True
            }
        })

@SOCKET_IO.on('LED_chase')
@catchLogExceptionsWrapper
def on_LED_chase(data):
    '''LED Solid Color Chase'''
    led_red = data['red']
    led_green = data['green']
    led_blue = data['blue']

    on_use_led_effect({
        'effect': "stripColor",
        'args': {
            'color': Color(led_red,led_green,led_blue),
#            'pattern': ColorPattern.CHASE,  # TODO implement chase animation pattern
            'pattern': ColorPattern.ALTERNATING,
            'time': 5
        }
    })

@SOCKET_IO.on('LED_RB')
@catchLogExceptionsWrapper
def on_LED_RB():
    '''LED rainbow'''
    on_use_led_effect({
        'effect': "rainbow",
        'args': {
            'time': 5
        }
    })

@SOCKET_IO.on('LED_RBCYCLE')
@catchLogExceptionsWrapper
def on_LED_RBCYCLE():
    '''LED rainbow Cycle'''
    on_use_led_effect({
        'effect': "rainbowCycle",
        'args': {
            'time': 5
        }
    })

@SOCKET_IO.on('LED_RBCHASE')
@catchLogExceptionsWrapper
def on_LED_RBCHASE():
    '''LED Rainbow Cycle Chase'''
    on_use_led_effect({
        'effect': "rainbowCycleChase",
        'args': {
            'time': 5
        }
    })

@SOCKET_IO.on('LED_brightness')
@catchLogExceptionsWrapper
def on_LED_brightness(data):
    '''Change LED Brightness'''
    brightness = data['brightness']
    strip.setBrightness(brightness)
    strip.show()
    RHData.set_option("ledBrightness", brightness)
    Events.trigger(Evt.LED_BRIGHTNESS_SET, {
        'level': brightness,
        })

@SOCKET_IO.on('set_option')
@catchLogExceptionsWrapper
def on_set_option(data):
    RHData.set_option(data['option'], data['value'])
    Events.trigger(Evt.OPTION_SET, {
        'option': data['option'],
        'value': data['value'],
        })

@SOCKET_IO.on('get_race_scheduled')
@catchLogExceptionsWrapper
def get_race_elapsed():
    # get current race status; never broadcasts to all
    emit('race_scheduled', {
        'scheduled': RACE.scheduled,
        'scheduled_at': RACE.scheduled_time
    })

@SOCKET_IO.on('save_callouts')
@catchLogExceptionsWrapper
def save_callouts(data):
    # save callouts to Options
    callouts = json.dumps(data['callouts'])
    RHData.set_option('voiceCallouts', callouts)
    logger.info('Set all voice callouts')
    logger.debug('Voice callouts set to: {0}'.format(callouts))

@SOCKET_IO.on('reload_callouts')
@catchLogExceptionsWrapper
def reload_callouts():
    RHUI.emit_callouts()

@SOCKET_IO.on('imdtabler_update_freqs')
@catchLogExceptionsWrapper
def imdtabler_update_freqs(data):
    ''' Update IMDTabler page with new frequencies list '''
    RHUI.emit_imdtabler_data(IMDTABLER_JAR_NAME, data['freq_list'].replace(',',' ').split())

@SOCKET_IO.on('clean_cache')
@catchLogExceptionsWrapper
def clean_results_cache():
    ''' wipe all results caches '''
    Results.invalidate_all_caches(RHData)
    PageCache.set_valid(False)

@SOCKET_IO.on('retry_secondary')
@catchLogExceptionsWrapper
def on_retry_secondary(data):
    '''Retry connection to secondary timer.'''
    CLUSTER.retrySecondary(data['secondary_id'])
    RHUI.emit_cluster_status()

# Socket io emit functions

@SOCKET_IO.on('get_pilotrace')
@catchLogExceptionsWrapper
def get_pilotrace(data):
    # get single race detail
    if 'pilotrace_id' in data:
        pilotrace = RHData.get_savedPilotRace(data['pilotrace_id'])

        laps = []
        for lap in RHData.get_savedRaceLaps_by_savedPilotRace(pilotrace.id):
            laps.append({
                    'id': lap.id,
                    'lap_time_stamp': lap.lap_time_stamp,
                    'lap_time': lap.lap_time,
                    'lap_time_formatted': lap.lap_time_formatted,
                    'source': lap.source,
                    'deleted': lap.deleted
                })

        pilot_data = RHData.get_pilot(pilotrace.pilot_id)
        if pilot_data:
            nodepilot = pilot_data.callsign
        else:
            nodepilot = None

        emit('race_details', {
            'pilotrace_id': data['pilotrace_id'],
            'callsign': nodepilot,
            'pilot_id': pilotrace.pilot_id,
            'node_index': pilotrace.node_index,
            'history_values': json.loads(pilotrace.history_values),
            'history_times': json.loads(pilotrace.history_times),
            'laps': laps,
            'enter_at': pilotrace.enter_at,
            'exit_at': pilotrace.exit_at,
        })


@SOCKET_IO.on('check_bpillfw_file')
@catchLogExceptionsWrapper
def check_bpillfw_file(data):
    fileStr = data['src_file_str']
    logger.debug("Checking node firmware file: " + fileStr)
    dataStr = None
    try:
        dataStr = stm32loader.load_source_file(fileStr, False)
    except Exception as ex:
        SOCKET_IO.emit('upd_set_info_text', "Error reading firmware file: {}<br><br><br><br>".format(ex))
        logger.debug("Error reading file '{}' in 'check_bpillfw_file()': {}".format(fileStr, ex))
        return
    try:  # find version, processor-type and build-timestamp strings in firmware '.bin' file
        rStr = RHUtils.findPrefixedSubstring(dataStr, INTERFACE.FW_VERSION_PREFIXSTR, \
                                             INTERFACE.FW_TEXT_BLOCK_SIZE)
        fwVerStr = rStr if rStr else "(unknown)"
        fwRTypStr = RHUtils.findPrefixedSubstring(dataStr, INTERFACE.FW_PROCTYPE_PREFIXSTR, \
                                             INTERFACE.FW_TEXT_BLOCK_SIZE)
        fwTypStr = (fwRTypStr + ", ") if fwRTypStr else ""
        rStr = RHUtils.findPrefixedSubstring(dataStr, INTERFACE.FW_BUILDDATE_PREFIXSTR, \
                                             INTERFACE.FW_TEXT_BLOCK_SIZE)
        if rStr:
            fwTimStr = rStr
            rStr = RHUtils.findPrefixedSubstring(dataStr, INTERFACE.FW_BUILDTIME_PREFIXSTR, \
                                                 INTERFACE.FW_TEXT_BLOCK_SIZE)
            if rStr:
                fwTimStr += " " + rStr
        else:
            fwTimStr = "unknown"
        fileSize = len(dataStr)
        logger.debug("Node update firmware file size={}, version={}, {}build timestamp: {}".\
                     format(fileSize, fwVerStr, fwTypStr, fwTimStr))
        infoStr = "Firmware update file size = {}<br>".format(fileSize) + \
                  "Firmware update version: {} ({}Build timestamp: {})<br><br>".\
                  format(fwVerStr, fwTypStr, fwTimStr)
        info_node = INTERFACE.get_info_node_obj()
        curNodeStr = info_node.firmware_version_str if info_node else None
        if curNodeStr:
            tsStr = info_node.firmware_timestamp_str
            if tsStr:
                curRTypStr = info_node.firmware_proctype_str
                ptStr = (curRTypStr + ", ") if curRTypStr else ""
                curNodeStr += " ({}Build timestamp: {})".format(ptStr, tsStr)
        else:
            curRTypStr = None
            curNodeStr = "(unknown)"
        infoStr += "Current firmware version: " + curNodeStr
        if fwRTypStr and curRTypStr and fwRTypStr != curRTypStr:
            infoStr += "<br><br><b>Warning</b>: Firmware file processor type ({}) does not match current ({})".\
                        format(fwRTypStr, curRTypStr)
        SOCKET_IO.emit('upd_set_info_text', infoStr)
        SOCKET_IO.emit('upd_enable_update_button')
    except Exception as ex:
        SOCKET_IO.emit('upd_set_info_text', "Error processing firmware file: {}<br><br><br><br>".format(ex))
        logger.exception("Error processing file '{}' in 'check_bpillfw_file()'".format(fileStr))

@SOCKET_IO.on('do_bpillfw_update')
@catchLogExceptionsWrapper
def do_bpillfw_update(data):
    srcStr = data['src_file_str']
    portStr = INTERFACE.get_fwupd_serial_name()
    msgStr = "Performing S32_BPill update, port='{}', file: {}".format(portStr, srcStr)
    logger.info(msgStr)
    SOCKET_IO.emit('upd_messages_init', (msgStr + "\n"))
    stop_background_threads()
    gevent.sleep(0.1)
    try:
        jump_to_node_bootloader()
        INTERFACE.close_fwupd_serial_port()
        s32Logger = logging.getLogger("stm32loader")
        def doS32Log(msgStr):  # send message to update-messages window and log file
            SOCKET_IO.emit('upd_messages_append', msgStr)
            gevent.idle()  # do thread yield to allow display updates
            s32Logger.info(msgStr)
            gevent.idle()  # do thread yield to allow display updates
            log.wait_for_queue_empty()
        stm32loader.set_console_output_fn(doS32Log)
        successFlag = stm32loader.flash_file_to_stm32(portStr, srcStr)
        msgStr = "Node update " + ("succeeded; restarting interface" \
                                   if successFlag else "failed")
        logger.info(msgStr)
        SOCKET_IO.emit('upd_messages_append', ("\n" + msgStr))
    except:
        logger.exception("Error in 'do_bpillfw_update()'")
    stm32loader.set_console_output_fn(None)
    gevent.sleep(0.2)
    logger.info("Reinitializing RH interface")
    ui_server_messages.clear()
    initialize_rh_interface()
    if RACE.num_nodes <= 0:
        SOCKET_IO.emit('upd_messages_append', "\nWarning: No receiver nodes found")
    buildServerInfo()
    reportServerInfo()
    init_race_state()
    start_background_threads(True)
    SOCKET_IO.emit('upd_messages_finish')  # show 'Close' button

@SOCKET_IO.on('set_vrx_node')
@catchLogExceptionsWrapper
def set_vrx_node(data):
    vrx_id = data['vrx_id']
    node = data['node']

    if vrx_manager.isEnabled():
        # TODO: vrx_manager.setDeviceMethod(device_id, method)
        # TODO: vrx_manager.setDevicePilot(device_id, pilot_id)

        vrx_manager.setDeviceSeat(vrx_id, node)

        # vrx_controller.set_seat_number(serial_num=vrx_id, desired_seat_num=node)
        logger.info("Set VRx {0} to node {1}".format(vrx_id, node))
    else:
        logger.error("Can't set VRx {0} to node {1}: Controller unavailable".format(vrx_id, node))

#
# Program Functions
#

def heartbeat_thread_function():
    '''Emits current rssi data, etc'''
    gevent.sleep(0.010)  # allow time for connection handshake to terminate before emitting data

    while True:
        try:
            node_data = INTERFACE.get_heartbeat_json()

            SOCKET_IO.emit('heartbeat', node_data)
            heartbeat_thread_function.iter_tracker += 1

            # update displayed IMD rating after freqs changed:
            if heartbeat_thread_function.imdtabler_flag and \
                    (heartbeat_thread_function.iter_tracker % HEARTBEAT_DATA_RATE_FACTOR) == 0:
                heartbeat_thread_function.imdtabler_flag = False
                RHUI.emit_imdtabler_rating(IMDTABLER_JAR_NAME)

            # emit rest of node data, but less often:
            if (heartbeat_thread_function.iter_tracker % (4*HEARTBEAT_DATA_RATE_FACTOR)) == 0:
                RHUI.emit_node_data()

            # emit cluster status less often:
            if (heartbeat_thread_function.iter_tracker % (4*HEARTBEAT_DATA_RATE_FACTOR)) == (2*HEARTBEAT_DATA_RATE_FACTOR):
                RHUI.emit_cluster_status()

            # collect vrx lock status
            if (heartbeat_thread_function.iter_tracker % (10*HEARTBEAT_DATA_RATE_FACTOR)) == 0:
                if vrx_manager.isEnabled():
                    vrx_manager.updateStatus()

            if (heartbeat_thread_function.iter_tracker % (10*HEARTBEAT_DATA_RATE_FACTOR)) == 4:
                # emit display status with offset
                if vrx_manager.isEnabled():
                    RHUI.emit_vrx_list()

            # emit environment data less often:
            if (heartbeat_thread_function.iter_tracker % (20*HEARTBEAT_DATA_RATE_FACTOR)) == 0:
                SENSORS.update_environmental_data()
                RHUI.emit_environmental_data()

            time_now = monotonic()

            # check if race is to be started
            if RACE.scheduled:
                if time_now > RACE.scheduled_time:
                    on_stage_race()
                    RACE.scheduled = False

            # if any comm errors then log them (at defined intervals; faster if debug mode)
            if time_now > heartbeat_thread_function.last_error_rep_time + \
                        (ERROR_REPORT_INTERVAL_SECS if not Config.GENERAL['DEBUG'] \
                        else ERROR_REPORT_INTERVAL_SECS/10):
                heartbeat_thread_function.last_error_rep_time = time_now
                rep_str = INTERFACE.get_intf_error_report_str()
                if rep_str:
                    logger.info(rep_str)

            gevent.sleep(0.500/HEARTBEAT_DATA_RATE_FACTOR)

        except KeyboardInterrupt:
            logger.info("Heartbeat thread terminated by keyboard interrupt")
            raise
        except SystemExit:
            raise
        except Exception:
            logger.exception('Exception in Heartbeat thread loop')
            gevent.sleep(0.500)

# declare/initialize variables for heartbeat functions
heartbeat_thread_function.iter_tracker = 0
heartbeat_thread_function.imdtabler_flag = False
heartbeat_thread_function.last_error_rep_time = monotonic()

@catchLogExceptionsWrapper
def clock_check_thread_function():
    ''' Monitor system clock and adjust PROGRAM_START_EPOCH_TIME if significant jump detected.
        (This can happen if NTP synchronization occurs after server starts up.) '''
    global PROGRAM_START_EPOCH_TIME
    global MTONIC_TO_EPOCH_MILLIS_OFFSET
    global serverInfoItems
    try:
        while True:
            gevent.sleep(10)
            if RACE.any_races_started:  # stop monitoring after any race started
                break
            time_now = monotonic()
            epoch_now = int((RHTimeFns.getUtcDateTimeNow() - EPOCH_START).total_seconds() * 1000)
            diff_ms = epoch_now - monotonic_to_epoch_millis(time_now)
            if abs(diff_ms) > 30000:
                PROGRAM_START_EPOCH_TIME += diff_ms
                MTONIC_TO_EPOCH_MILLIS_OFFSET = epoch_now - 1000.0*time_now
                logger.info("Adjusting PROGRAM_START_EPOCH_TIME for shift in system clock ({0:.1f} secs) to: {1:.0f}".\
                            format(diff_ms/1000, PROGRAM_START_EPOCH_TIME))
                # update values that will be reported if running as cluster timer
                serverInfoItems['prog_start_epoch'] = "{0:.0f}".format(PROGRAM_START_EPOCH_TIME)
                serverInfoItems['prog_start_time'] = str(datetime.utcfromtimestamp(PROGRAM_START_EPOCH_TIME/1000.0))
                if CLUSTER.has_joined_cluster():
                    logger.debug("Emitting 'join_cluster_response' message with updated 'prog_start_epoch'")
                    CLUSTER.emit_join_cluster_response(SOCKET_IO, serverInfoItems)
    except KeyboardInterrupt:
        logger.info("clock_check_thread terminated by keyboard interrupt")
        raise
    except SystemExit:
        raise
    except Exception:
        logger.exception('Exception in clock_check_thread')

def ms_from_race_start():
    '''Return milliseconds since race start.'''
    delta_time = monotonic() - RACE.start_time_monotonic
    milli_sec = delta_time * 1000.0
    return milli_sec

def ms_to_race_start():
    '''Return milliseconds since race start.'''
    if RACE.scheduled:
        delta_time = monotonic() - RACE.scheduled_time
        milli_sec = delta_time * 1000.0
        return milli_sec
    else:
        return None

def ms_from_program_start():
    '''Returns the elapsed milliseconds since the start of the program.'''
    delta_time = monotonic() - PROGRAM_START_MTONIC
    milli_sec = delta_time * 1000.0
    return milli_sec

def pass_record_callback(node, lap_timestamp_absolute, source):
    PassInvokeFuncQueueObj.put(do_pass_record_callback, node, lap_timestamp_absolute, source)

def do_pass_record_callback(node, lap_timestamp_absolute, source):
    '''Handles pass records from the nodes.'''

    logger.debug('Pass record: Node={}, abs_ts={:.3f}, source={} ("{}")' \
                 .format(node.index+1, lap_timestamp_absolute, source, INTERFACE.get_lap_source_str(source)))
    node.pass_crossing_flag = False  # clear the "synchronized" version of the crossing flag
    node.debug_pass_count += 1
    RHUI.emit_node_data() # For updated triggers and peaks

    profile_freqs = json.loads(RACE.profile.frequencies)
    if profile_freqs["f"][node.index] != RHUtils.FREQUENCY_ID_NONE :
        # always count laps if race is running, otherwise test if lap should have counted before race end
        if RACE.race_status is RaceStatus.RACING \
            or (RACE.race_status is RaceStatus.DONE and \
                lap_timestamp_absolute < RACE.end_time):

            # Get the current pilot id on the node
            pilot_id = RHData.get_pilot_from_heatNode(RACE.current_heat, node.index)

            # reject passes before race start and with disabled (no-pilot) nodes
            race_format = RACE.format
            if (pilot_id is not None and pilot_id != RHUtils.PILOT_ID_NONE) or race_format is SECONDARY_RACE_FORMAT or RACE.current_heat is RHUtils.HEAT_ID_NONE:
                if lap_timestamp_absolute >= RACE.start_time_monotonic:

                    # if node EnterAt/ExitAt values need to be restored then do it soon
                    if node.start_thresh_lower_flag:
                        node.start_thresh_lower_time = monotonic()

                    lap_time_stamp = (lap_timestamp_absolute - RACE.start_time_monotonic)
                    lap_time_stamp *= 1000 # store as milliseconds

                    lap_number = len(RACE.get_active_laps()[node.index])

                    if lap_number: # This is a normal completed lap
                        # Find the time stamp of the last lap completed (including "late" laps for timing)
                        last_lap_time_stamp = RACE.get_active_laps(True)[node.index][-1]['lap_time_stamp']

                        # New lap time is the difference between the current time stamp and the last
                        lap_time = lap_time_stamp - last_lap_time_stamp

                    else: # No previous laps, this is the first pass
                        # Lap zero represents the time from the launch pad to flying through the gate
                        lap_time = lap_time_stamp
                        node.first_cross_flag = True  # indicate first crossing completed

                    if race_format is SECONDARY_RACE_FORMAT:
                        min_lap = 0  # don't enforce min-lap time if running as secondary timer
                        min_lap_behavior = 0
                    else:
                        min_lap = RHData.get_optionInt("MinLapSec")
                        min_lap_behavior = RHData.get_optionInt("MinLapBehavior")

                    lap_time_fmtstr = RHUtils.time_format(lap_time, RHData.get_option('timeFormat'))
                    lap_ts_fmtstr = RHUtils.time_format(lap_time_stamp, RHData.get_option('timeFormat'))
                    pilot_obj = RHData.get_pilot(pilot_id)
                    pilot_namestr = pilot_obj.callsign if pilot_obj else ""

                    lap_ok_flag = True
                    lap_late_flag = False
                    if lap_number != 0:  # if initial lap then always accept and don't check lap time; else:
                        if lap_time < (min_lap * 1000):  # if lap time less than minimum
                            node.under_min_lap_count += 1
                            logger.info('Pass record under lap minimum ({}): Node={}, lap={}, lapTime={}, sinceStart={}, count={}, source={}, pilot: {}' \
                                       .format(min_lap, node.index+1, lap_number, \
                                               lap_time_fmtstr, lap_ts_fmtstr, \
                                               node.under_min_lap_count, INTERFACE.get_lap_source_str(source), \
                                               pilot_namestr))
                            if min_lap_behavior != 0:  # if behavior is 'Discard New Short Laps'
                                lap_ok_flag = False

                        if race_format.race_mode == 0 and \
                            race_format.lap_grace_sec > -1 and \
                            lap_time_stamp > (race_format.race_time_sec + race_format.lap_grace_sec)*1000:
                            logger.info('Ignoring lap after grace period expired: Node={}, lap={}, lapTime={}, sinceStart={}, source={}, pilot: {}' \
                                       .format(node.index+1, lap_number, lap_time_fmtstr, lap_ts_fmtstr, \
                                               INTERFACE.get_lap_source_str(source), pilot_namestr))
                            lap_ok_flag = False

                    if lap_ok_flag:
                        node_finished_flag = RACE.get_node_finished_flag(node.index)
                        # set next node race status as 'finished' if timer mode is count-down race and race-time has expired
                        if (race_format.race_mode == 0 and lap_time_stamp > race_format.race_time_sec * 1000) or \
                            (RACE.format.win_condition == WinCondition.FIRST_TO_LAP_X and lap_number >= race_format.number_laps_win):
                            RACE.set_node_finished_flag(node.index)
                            if not node_finished_flag:
                                logger.info('Pilot {} done'.format(pilot_obj.callsign if pilot_obj else node.index))
                                Events.trigger(Evt.RACE_PILOT_DONE, {
                                    'node_index': node.index,
                                    'color': led_manager.getDisplayColor(node.index),
                                    })

                        if node_finished_flag:
                            lap_late_flag = True  # "late" lap pass (after grace lap)
                            logger.info('Ignoring lap after pilot done: Node={}, lap={}, lapTime={}, sinceStart={}, source={}, pilot: {}' \
                                       .format(node.index+1, lap_number, lap_time_fmtstr, lap_ts_fmtstr, \
                                               INTERFACE.get_lap_source_str(source), pilot_namestr))
                            
                        if RACE.win_status == WinStatus.DECLARED and \
                            race_format.race_mode == 1 and \
                            RACE.format.team_racing_mode and \
                            RACE.format.win_condition == WinCondition.FIRST_TO_LAP_X:
                            lap_late_flag = True  # "late" lap pass after team race winner declared (when no time limit)
                            if pilot_obj:
                                t_str = ", Team " + pilot_obj.team
                            else:
                                t_str = ""
                            logger.info('Ignoring lap after race winner declared: Node={}, lap={}, lapTime={}, sinceStart={}, source={}, pilot: {}{}' \
                                       .format(node.index+1, lap_number, lap_time_fmtstr, lap_ts_fmtstr, \
                                               INTERFACE.get_lap_source_str(source), pilot_namestr, t_str))

                        if logger.getEffectiveLevel() <= logging.DEBUG:  # if DEBUG msgs actually being logged
                            late_str = " (late lap)" if lap_late_flag else ""
                            enter_fmtstr = RHUtils.time_format((node.enter_at_timestamp-RACE.start_time_monotonic)*1000, \
                                                               RHData.get_option('timeFormat')) \
                                           if node.enter_at_timestamp else "0"
                            exit_fmtstr = RHUtils.time_format((node.exit_at_timestamp-RACE.start_time_monotonic)*1000, \
                                                              RHData.get_option('timeFormat')) \
                                           if node.exit_at_timestamp else "0"
                            logger.debug('Lap pass{}: Node={}, lap={}, lapTime={}, sinceStart={}, abs_ts={:.3f}, source={}, enter={}, exit={}, dur={:.0f}ms, pilot: {}' \
                                        .format(late_str, node.index+1, lap_number, lap_time_fmtstr, lap_ts_fmtstr, \
                                                lap_timestamp_absolute, INTERFACE.get_lap_source_str(source), \
                                                enter_fmtstr, exit_fmtstr, \
                                                (node.exit_at_timestamp-node.enter_at_timestamp)*1000, pilot_namestr))

                        # emit 'pass_record' message (to primary timer in cluster, livetime, etc).
                        RHUI.emit_pass_record(node, lap_time_stamp)

                        # Add the new lap to the database
                        lap_data = {
                            'lap_number': lap_number,
                            'lap_time_stamp': lap_time_stamp,
                            'lap_time': lap_time,
                            'lap_time_formatted': lap_time_fmtstr,
                            'source': source,
                            'deleted': lap_late_flag,  # delete if lap pass is after race winner declared
                            'late_lap': lap_late_flag
                        }
                        RACE.node_laps[node.index].append(lap_data)

                        RACE.clear_results()

                        Events.trigger(Evt.RACE_LAP_RECORDED, {
                            'node_index': node.index,
                            'color': led_manager.getDisplayColor(node.index),
                            'lap': lap_data,
                            'results': RACE.get_results(RHData)
                            })

                        RHUI.emit_current_laps() # update all laps on the race page
                        RHUI.emit_current_leaderboard() # generate and update leaderboard

                        if lap_number == 0:
                            RHUI.emit_first_pass_registered(node.index) # play first-pass sound

                        if race_format.start_behavior == StartBehavior.FIRST_LAP:
                            lap_number += 1

                        # announce lap
                        if lap_number > 0:
                            check_leader = race_format.win_condition != WinCondition.NONE and \
                                           RACE.win_status != WinStatus.DECLARED
                            # announce pilot lap number unless winner declared and pilot has finished final lap
                            lap_id = lap_number if RACE.win_status != WinStatus.DECLARED or \
                                                   (not node_finished_flag) else None
                            if race_format.team_racing_mode:
                                team_name = pilot_obj.team if pilot_obj else ""
                                team_laps = RACE.team_results['meta']['teams'][team_name]['laps']
                                if not lap_late_flag:
                                    logger.debug('Lap pass: Node={}, lap={}, pilot={} -> Team {} lap {}' \
                                          .format(node.index+1, lap_number, pilot_namestr, team_name, team_laps))
                                # if winning team has been declared then don't announce team lap number
                                if RACE.win_status == WinStatus.DECLARED:
                                    team_laps = None
                                RHUI.emit_phonetic_data(pilot_id, lap_id, lap_time, team_name, team_laps, \
                                                (check_leader and \
                                                 team_name == Results.get_leading_team_name(RACE.team_results)), \
                                                node_finished_flag, node.index)
                            else:
                                RHUI.emit_phonetic_data(pilot_id, lap_id, lap_time, None, None, \
                                                (check_leader and \
                                                 pilot_id == Results.get_leading_pilot_id(RACE.results)), \
                                                node_finished_flag, node.index)

                            # check for and announce possible winner (but wait until pass-record processing(s) is finished)
                            PassInvokeFuncQueueObj.put(check_win_condition, emit_leaderboard_on_win=True) 

                    else:
                        # record lap as 'invalid'
                        RACE.node_laps[node.index].append({
                            'lap_number': lap_number,
                            'lap_time_stamp': lap_time_stamp,
                            'lap_time': lap_time,
                            'lap_time_formatted': lap_time_fmtstr,
                            'source': source,
                            'deleted': True,
                            'invalid': True
                        })
                else:
                    logger.debug('Pass record dismissed: Node {}, Race not started (abs_ts={:.3f}, source={})' \
                        .format(node.index+1, lap_timestamp_absolute, INTERFACE.get_lap_source_str(source)))
            else:
                logger.debug('Pass record dismissed: Node {}, Pilot not defined (abs_ts={:.3f}, source={})' \
                    .format(node.index+1, lap_timestamp_absolute, INTERFACE.get_lap_source_str(source)))
    else:
        logger.debug('Pass record dismissed: Node {}, Frequency not defined (abs_ts={:.3f}, source={})' \
            .format(node.index+1, lap_timestamp_absolute, INTERFACE.get_lap_source_str(source)))

def check_win_condition(**kwargs):
    previous_win_status = RACE.win_status
    win_not_decl_flag = RACE.win_status in [WinStatus.NONE, WinStatus.PENDING_CROSSING, WinStatus.OVERTIME]
    del_lap_flag = 'deletedLap' in kwargs

    # if winner not yet declared or racer lap was deleted then check win condition
    win_status_dict = Results.check_win_condition_result(RACE, RHData, INTERFACE, **kwargs) \
                      if win_not_decl_flag or del_lap_flag else None

    if win_status_dict is not None:
        race_format = RACE.format
        RACE.win_status = win_status_dict['status']

        if RACE.win_status != WinStatus.NONE and logger.getEffectiveLevel() <= logging.DEBUG:
            logger.debug("Pilot lap counts: " + Results.get_pilot_lap_counts_str(RACE.results))
            if race_format.team_racing_mode:
                logger.debug("Team lap totals: " + Results.get_team_lap_totals_str(RACE.team_results))

        # if racer lap was deleted and result is winner un-declared
        if del_lap_flag and RACE.win_status != previous_win_status and \
                            RACE.win_status == WinStatus.NONE:
            RACE.win_status = WinStatus.NONE
            RACE.status_message = ''
            logger.info("Race status msg:  <None>")
            return win_status_dict

        if win_status_dict['status'] == WinStatus.DECLARED:
            # announce winner
            win_data = win_status_dict['data']
            if race_format.team_racing_mode:
                win_str = win_data.get('name', '')
                status_msg_str = __('Winner is') + ' ' + __('Team') + ' ' + win_str
                log_msg_str = "Race status msg:  Winner is Team " + win_str
                phonetic_str = status_msg_str
            else:
                win_str = win_data.get('callsign', '')
                status_msg_str = __('Winner is') + ' ' + win_str
                log_msg_str = "Race status msg:  Winner is " + win_str
                if 'pilot_id' in win_data and win_data['pilot_id'] is not None:
                    win_phon_name = RHData.get_pilot(win_data['pilot_id']).phonetic
                elif win_data['callsign']:
                    win_phon_name = win_data['callsign']
                else:
                    win_phon_name = None
                if (not win_phon_name) or len(win_phon_name) <= 0:  # if no phonetic then use callsign
                    win_phon_name = win_data.get('callsign', '')
                phonetic_str = __('Winner is') + ' ' + win_phon_name

            # if racer lap was deleted then only output if win-status details changed
            if (not del_lap_flag) or RACE.win_status != previous_win_status or \
                                        status_msg_str != RACE.status_message:
                RACE.status_message = status_msg_str
                logger.info(log_msg_str)
                RHUI.emit_phonetic_text(phonetic_str, 'race_winner', True)
                Events.trigger(Evt.RACE_WIN, {
                    'win_status': win_status_dict,
                    'message': RACE.status_message,
                    'node_index': win_data.get('node', None),
                    'color': led_manager.getDisplayColor(win_data['node']) \
                                            if 'node' in win_data else None,
                    'results': RACE.results
                    })

        elif win_status_dict['status'] == WinStatus.TIE:
            # announce tied
            if win_status_dict['status'] != previous_win_status:
                RACE.status_message = __('Race Tied')
                logger.info("Race status msg:  Race Tied")
                RHUI.emit_phonetic_text(RACE.status_message, 'race_winner')
        elif win_status_dict['status'] == WinStatus.OVERTIME:
            # announce overtime
            if win_status_dict['status'] != previous_win_status:
                RACE.status_message = __('Race Tied: Overtime')
                logger.info("Race status msg:  Race Tied: Overtime")
                RHUI.emit_phonetic_text(RACE.status_message, 'race_winner')

        if 'max_consideration' in win_status_dict:
            logger.info("Waiting {0}ms to declare winner.".format(win_status_dict['max_consideration']))
            gevent.sleep(win_status_dict['max_consideration'] / 1000)
            if 'start_token' in kwargs and RACE.start_token == kwargs['start_token']:
                logger.info("Maximum win condition consideration time has expired.")
                check_win_condition(forced=True)

        if 'emit_leaderboard_on_win' in kwargs:
            if RACE.win_status != WinStatus.NONE:
                RHUI.emit_current_leaderboard()  # show current race status on leaderboard
    
    return win_status_dict

@catchLogExcDBCloseWrapper
def new_enter_or_exit_at_callback(node, is_enter_at_flag):
    gevent.sleep(0.025)  # delay to avoid potential I/O error
    if is_enter_at_flag:
        logger.info('Finished capture of enter-at level for node {0}, level={1}, count={2}'.format(node.index+1, node.enter_at_level, node.cap_enter_at_count))
        on_set_enter_at_level({
            'node': node.index,
            'enter_at_level': node.enter_at_level
        })
        RHUI.emit_enter_at_level(node)
    else:
        logger.info('Finished capture of exit-at level for node {0}, level={1}, count={2}'.format(node.index+1, node.exit_at_level, node.cap_exit_at_count))
        on_set_exit_at_level({
            'node': node.index,
            'exit_at_level': node.exit_at_level
        })
        RHUI.emit_exit_at_level(node)

@catchLogExcDBCloseWrapper
def node_crossing_callback(node):
    RHUI.emit_node_crossing_change(node)
    # handle LED gate-status indicators:

    if RACE.race_status == RaceStatus.RACING:  # if race is in progress
        # if pilot assigned to node and first crossing is complete
        if RACE.format is SECONDARY_RACE_FORMAT or (
            node.current_pilot_id != RHUtils.PILOT_ID_NONE and node.first_cross_flag):
            # first crossing has happened; if 'enter' then show indicator,
            #  if first event is 'exit' then ignore (because will be end of first crossing)
            if node.crossing_flag:
                Events.trigger(Evt.CROSSING_ENTER, {
                    'nodeIndex': node.index,
                    'color': led_manager.getDisplayColor(node.index)
                    })
                node.show_crossing_flag = True
            else:
                if node.show_crossing_flag:
                    Events.trigger(Evt.CROSSING_EXIT, {
                        'nodeIndex': node.index,
                        'color': led_manager.getDisplayColor(node.index)
                        })
                else:
                    node.show_crossing_flag = True

def default_frequencies():
    '''Set node frequencies, R1367 for 4, IMD6C+ for 5+.'''
    if RACE.num_nodes < 5:
        freqs = {
            'b': ['R', 'R', 'R', 'R', None, None, None, None],
            'c': [1, 3, 6, 7, None, None, None, None],
            'f': [5658, 5732, 5843, 5880, RHUtils.FREQUENCY_ID_NONE, RHUtils.FREQUENCY_ID_NONE, RHUtils.FREQUENCY_ID_NONE, RHUtils.FREQUENCY_ID_NONE]
        }
    else:
        freqs = {
            'b': ['R', 'R', 'F', 'F', 'R', 'R', None, None],
            'c': [1, 2, 2, 4, 7, 8, None, None],
            'f': [5658, 5695, 5760, 5800, 5880, 5917, RHUtils.FREQUENCY_ID_NONE, RHUtils.FREQUENCY_ID_NONE]
        }

        while RACE.num_nodes > len(freqs['f']):
            freqs['b'].append(None)
            freqs['c'].append(None)
            freqs['f'].append(RHUtils.FREQUENCY_ID_NONE)

    return freqs

def assign_frequencies():
    '''Assign frequencies to nodes'''
    profile = RACE.profile
    freqs = json.loads(profile.frequencies)

    for idx in range(RACE.num_nodes):
        INTERFACE.set_frequency(idx, freqs["f"][idx])
        RACE.clear_results()
        Events.trigger(Evt.FREQUENCY_SET, {
            'nodeIndex': idx,
            'frequency': freqs["f"][idx],
            'band': freqs["b"][idx],
            'channel': freqs["c"][idx]
            })

        logger.info('Frequency set: Node {0} B:{1} Ch:{2} Freq:{3}'.format(idx+1, freqs["b"][idx], freqs["c"][idx], freqs["f"][idx]))

def emit_current_log_file_to_socket():
    if Current_log_path_name:
        try:
            with io.open(Current_log_path_name, 'r') as f:
                SOCKET_IO.emit("hardware_log_init", f.read())
        except Exception:
            logger.exception("Error sending current log file to socket")
    log.start_socket_forward_handler()

def db_init(nofill=False):
    '''Initialize database.'''
    RHData.db_init(nofill)
    reset_current_laps()
    RACE.format = RHData.get_first_raceFormat()
    RHUI.emit_current_laps()
    assign_frequencies()
    Events.trigger(Evt.DATABASE_INITIALIZE)
    logger.info('Database initialized')

def db_reset():
    '''Resets database.'''
    RHData.reset_all()
    reset_current_laps()
    RACE.format = RHData.get_first_raceFormat()
    RHUI.emit_current_laps()
    assign_frequencies()
    logger.info('Database reset')

def reset_current_laps():
    '''Resets database current laps to default.'''
    RACE.node_laps = {}
    for idx in range(RACE.num_nodes):
        RACE.node_laps[idx] = []

    RACE.clear_results()
    logger.debug('Database current laps reset')

def expand_heats():
    ''' ensure loaded data includes enough slots for current nodes '''
    for heat in RHData.get_heats():
        heatNodes = RHData.get_heatNodes_by_heat(heat.id)
        while len(heatNodes) < RACE.num_nodes:
            heatNodes = RHData.get_heatNodes_by_heat(heat.id)
            RHData.add_heatNode(heat.id, None)

def init_race_state():
    expand_heats()

    # Send profile values to nodes
    on_set_profile({'profile': RACE.profile.id}, False)

    # Set race format
    RACE.format = RHData.get_first_raceFormat()

    # Init laps
    reset_current_laps()

    # Set current heat
    finalize_current_heat_set(RHData.get_first_safe_heat_id())

    # Normalize results caches
    PageCache.set_valid(False)

def init_interface_state(startup=False):
    # Cancel current race
    if startup:
        RACE.race_status = RaceStatus.READY # Go back to ready state
        INTERFACE.set_race_status(RaceStatus.READY)
        Events.trigger(Evt.LAPS_CLEAR)
        RACE.timer_running = False # indicate race timer not running
        RACE.scheduled = False # also stop any deferred start
        SOCKET_IO.emit('stop_timer')
    else:
        on_discard_laps()
    # Reset laps display
    reset_current_laps()

def init_LED_effects():
    # start with defaults
    effects = {
        Evt.RACE_STAGE: "stripColor2_1",
        Evt.RACE_START: "stripColorSolid",
        Evt.RACE_FINISH: "stripColor4_4",
        Evt.RACE_STOP: "stripColorSolid",
        Evt.LAPS_CLEAR: "clear",
        Evt.CROSSING_ENTER: "stripSparkle",
        Evt.CROSSING_EXIT: "none",
        Evt.RACE_LAP_RECORDED: "none",
        Evt.RACE_WIN: "none",
        Evt.MESSAGE_STANDARD: "none",
        Evt.MESSAGE_INTERRUPT: "none",
        Evt.STARTUP: "rainbowCycle",
        Evt.SHUTDOWN: "clear",
        LEDEvent.IDLE_DONE: "clear",
        LEDEvent.IDLE_READY: "clear",
        LEDEvent.IDLE_RACING: "clear",
    }
    if "bitmapRHLogo" in led_manager.getRegisteredEffects() and Config.LED['LED_ROWS'] > 1:
        effects[Evt.STARTUP] = "bitmapRHLogo"
        effects[Evt.RACE_STAGE] = "bitmapOrangeEllipsis"
        effects[Evt.RACE_START] = "bitmapGreenArrow"
        effects[Evt.RACE_FINISH] = "bitmapCheckerboard"
        effects[Evt.RACE_STOP] = "bitmapRedX"

    # update with DB values (if any)
    effect_opt = RHData.get_option('ledEffects')
    if effect_opt:
        effects.update(json.loads(effect_opt))
    # set effects
    led_manager.setEventEffect("manualColor", "stripColor")
    for item in effects:
        led_manager.setEventEffect(item, effects[item])


def determineHostAddress(maxRetrySecs=10):
    ''' Determines local host IP address.  Will wait and retry to get valid IP, in
        case system is starting up and needs time to connect to network and DHCP. '''
    global server_ipaddress_str
    if server_ipaddress_str:
        return server_ipaddress_str  # if previously determined then return value
    sTime = monotonic()
    while True:
        try:
            ipAddrStr = RHUtils.getLocalIPAddress()
            if ipAddrStr and ipAddrStr != "127.0.0.1":  # don't accept default-localhost IP
                server_ipaddress_str = ipAddrStr
                break
            logger.debug("Querying of host IP address returned " + ipAddrStr)
        except Exception as ex:
            logger.debug("Error querying host IP address: " + str(ex))
        if monotonic() > sTime + maxRetrySecs:
            ipAddrStr = "0.0.0.0"
            logger.warning("Unable to determine IP address for host machine")
            break
        gevent.sleep(1)
    try:
        hNameStr = socket.gethostname()
    except Exception as ex:
        logger.info("Error querying hostname: " + str(ex))
        hNameStr = "UNKNOWN"
    logger.info("Host machine is '{0}' at {1}".format(hNameStr, ipAddrStr))
    return ipAddrStr

def jump_to_node_bootloader():
    try:
        INTERFACE.jump_to_bootloader()
    except Exception:
        logger.error("Error executing jump to node bootloader")

def shutdown_button_thread_fn():
    try:
        logger.debug("Started shutdown-button-handler thread")
        idleCntr = 0
        while True:
            gevent.sleep(0.050)
            if not ShutdownButtonInputHandler.isEnabled():  # if button handler disabled
                break                                       #  then exit thread
            # poll button input and invoke callbacks
            bStatFlg = ShutdownButtonInputHandler.pollProcessInput(monotonic())
            # while background thread not started and button not pressed
            #  send periodic server-idle messages to node
            if (HEARTBEAT_THREAD is None) and BACKGROUND_THREADS_ENABLED and INTERFACE:
                idleCntr += 1
                if idleCntr >= 74:
                    if idleCntr >= 80:
                        idleCntr = 0    # show pattern on node LED via messages
                    if (not bStatFlg) and (idleCntr % 2 == 0):
                        INTERFACE.send_server_idle_message()
    except KeyboardInterrupt:
        logger.info("shutdown_button_thread_fn terminated by keyboard interrupt")
        raise
    except SystemExit:
        raise
    except Exception:
        logger.exception("Exception error in 'shutdown_button_thread_fn()'")
    logger.debug("Exited shutdown-button-handler thread")

def start_shutdown_button_thread():
    if ShutdownButtonInputHandler and not ShutdownButtonInputHandler.isEnabled():
        ShutdownButtonInputHandler.setEnabled(True)
        gevent.spawn(shutdown_button_thread_fn)

def stop_shutdown_button_thread():
    if ShutdownButtonInputHandler:
        ShutdownButtonInputHandler.setEnabled(False)

def shutdown_button_pressed():
    logger.debug("Detected shutdown button pressed")
    INTERFACE.send_shutdown_button_state(1)

def shutdown_button_released(longPressReachedFlag):
    logger.debug("Detected shutdown button released, longPressReachedFlag={}".\
                format(longPressReachedFlag))
    if not longPressReachedFlag:
        INTERFACE.send_shutdown_button_state(0)

def shutdown_button_long_press():
    logger.info("Detected shutdown button long press; performing shutdown now")
    on_shutdown_pi()

def _do_init_rh_interface():
    try:
        global INTERFACE
        rh_interface_name = os.environ.get('RH_INTERFACE', 'RH') + "Interface"
        try:
            logger.debug("Initializing interface module: " + rh_interface_name)
            interfaceModule = importlib.import_module(rh_interface_name)
            INTERFACE = interfaceModule.get_hardware_interface(config=Config, \
                            isS32BPillFlag=RHGPIO.isS32BPillBoard(), **hardwareHelpers)
            # if no nodes detected, system is RPi, not S32_BPill, and no serial port configured
            #  then check if problem is 'smbus2' or 'gevent' lib not installed
            if INTERFACE and ((not INTERFACE.nodes) or len(INTERFACE.nodes) <= 0) and \
                        RHUtils.isSysRaspberryPi() and (not RHGPIO.isS32BPillBoard()) and \
                        ((not Config.SERIAL_PORTS) or len(Config.SERIAL_PORTS) <= 0):
                try:
                    importlib.import_module('smbus2')
                    importlib.import_module('gevent')
                except ImportError:
                    logger.warning("Unable to import libraries for I2C nodes; try:  " +\
                                   "sudo pip install --upgrade --no-cache-dir -r requirements.txt")
                    set_ui_message(
                        'i2c',
                        __("Unable to import libraries for I2C nodes. Try: <code>sudo pip install --upgrade --no-cache-dir -r requirements.txt</code>"),
                        header='Warning',
                        subclass='no-library'
                        )
                RACE.num_nodes = 0
                INTERFACE.pass_record_callback = pass_record_callback
                INTERFACE.new_enter_or_exit_at_callback = new_enter_or_exit_at_callback
                INTERFACE.node_crossing_callback = node_crossing_callback
                return True
        except (ImportError, RuntimeError, IOError) as ex:
            logger.info('Unable to initialize nodes via ' + rh_interface_name + ':  ' + str(ex))
        if (not INTERFACE) or (not INTERFACE.nodes) or len(INTERFACE.nodes) <= 0:
            if (not Config.SERIAL_PORTS) or len(Config.SERIAL_PORTS) <= 0:
                interfaceModule = importlib.import_module('MockInterface')
                INTERFACE = interfaceModule.get_hardware_interface(config=Config, **hardwareHelpers)
                for node in INTERFACE.nodes:  # put mock nodes at latest API level
                    node.api_level = NODE_API_BEST
                set_ui_message(
                    'mock',
                    __("Server is using simulated (mock) nodes"),
                    header='Notice',
                    subclass='in-use'
                    )
            else:
                try:
                    importlib.import_module('serial')
                    if INTERFACE:
                        if not (getattr(INTERFACE, "get_info_node_obj") and INTERFACE.get_info_node_obj()):
                            logger.info("Unable to initialize serial node(s): {0}".format(Config.SERIAL_PORTS))
                            logger.info("If an S32_BPill board is connected, its processor may need to be flash-updated")
                            # enter serial port name so it's available for node firmware update
                            if getattr(INTERFACE, "set_mock_fwupd_serial_obj"):
                                INTERFACE.set_mock_fwupd_serial_obj(Config.SERIAL_PORTS[0])
                                set_ui_message('stm32', \
                                     __("Server is unable to communicate with node processor") + ". " + \
                                          __("If an S32_BPill board is connected, you may attempt to") + \
                                          " <a href=\"/updatenodes\">" + __("flash-update") + "</a> " + \
                                          __("its processor."), \
                                    header='Warning', subclass='no-comms')
                    else:
                        logger.info("Unable to initialize specified serial node(s): {0}".format(Config.SERIAL_PORTS))
                        return False  # unable to open serial port
                except ImportError:
                    logger.info("Unable to import library for serial node(s) - is 'pyserial' installed?")
                    return False

        RACE.num_nodes = len(INTERFACE.nodes)  # save number of nodes found
        # set callback functions invoked by interface module
        INTERFACE.pass_record_callback = pass_record_callback
        INTERFACE.new_enter_or_exit_at_callback = new_enter_or_exit_at_callback
        INTERFACE.node_crossing_callback = node_crossing_callback
        RHUI._interface = INTERFACE
        return True
    except:
        logger.exception("Error initializing RH interface")
        return False

def initialize_rh_interface():
    if not _do_init_rh_interface():
        return False
    if RACE.num_nodes == 0:
        logger.warning('*** WARNING: NO RECEIVER NODES FOUND ***')
        set_ui_message(
            'node',
            __("No receiver nodes found"),
            header='Warning',
            subclass='none'
            )
    return True

# Create and save server/node information
def buildServerInfo():
    global serverInfo
    global serverInfoItems
    try:
        serverInfo = {}

        serverInfo['about_html'] = "<ul>"

        # Release Version
        serverInfo['release_version'] = RELEASE_VERSION
        serverInfo['about_html'] += "<li>" + __("Version") + ": " + str(RELEASE_VERSION) + "</li>"

        # Server API
        serverInfo['server_api'] = SERVER_API
        serverInfo['about_html'] += "<li>" + __("Server API") + ": " + str(SERVER_API) + "</li>"

        # Server API
        serverInfo['json_api'] = JSON_API

        # Node API levels
        node_api_level = 0
        serverInfo['node_api_match'] = True

        serverInfo['node_api_lowest'] = 0
        serverInfo['node_api_levels'] = [None]

        info_node = INTERFACE.get_info_node_obj()
        if info_node:
            if info_node.api_level:
                node_api_level = info_node.api_level
                serverInfo['node_api_lowest'] = node_api_level
                if len(INTERFACE.nodes):
                    serverInfo['node_api_levels'] = []
                    for node in INTERFACE.nodes:
                        serverInfo['node_api_levels'].append(node.api_level)
                        if node.api_level != node_api_level:
                            serverInfo['node_api_match'] = False
                        if node.api_level < serverInfo['node_api_lowest']:
                            serverInfo['node_api_lowest'] = node.api_level
                    # if multi-node and all api levels same then only include one entry
                    if serverInfo['node_api_match'] and INTERFACE.nodes[0].multi_node_index >= 0:
                        serverInfo['node_api_levels'] = serverInfo['node_api_levels'][0:1]
                else:
                    serverInfo['node_api_levels'] = [node_api_level]

        serverInfo['about_html'] += "<li>" + __("Node API") + ": "
        if node_api_level:
            if serverInfo['node_api_match']:
                serverInfo['about_html'] += str(node_api_level)
            else:
                serverInfo['about_html'] += "[ "
                for idx, level in enumerate(serverInfo['node_api_levels']):
                    serverInfo['about_html'] += str(idx+1) + ":" + str(level) + " "
                serverInfo['about_html'] += "]"
        else:
            serverInfo['about_html'] += "None (Delta5)"

        serverInfo['about_html'] += "</li>"

        # Node firmware versions
        node_fw_version = None
        serverInfo['node_version_match'] = True
        serverInfo['node_fw_versions'] = [None]
        if info_node:
            if info_node.firmware_version_str:
                node_fw_version = info_node.firmware_version_str
                if len(INTERFACE.nodes):
                    serverInfo['node_fw_versions'] = []
                    for node in INTERFACE.nodes:
                        serverInfo['node_fw_versions'].append(\
                                node.firmware_version_str if node.firmware_version_str else "0")
                        if node.firmware_version_str != node_fw_version:
                            serverInfo['node_version_match'] = False
                    # if multi-node and all versions same then only include one entry
                    if serverInfo['node_version_match'] and INTERFACE.nodes[0].multi_node_index >= 0:
                        serverInfo['node_fw_versions'] = serverInfo['node_fw_versions'][0:1]
                else:
                    serverInfo['node_fw_versions'] = [node_fw_version]
        if node_fw_version:
            serverInfo['about_html'] += "<li>" + __("Node Version") + ": "
            if serverInfo['node_version_match']:
                serverInfo['about_html'] += str(node_fw_version)
            else:
                serverInfo['about_html'] += "[ "
                for idx, ver in enumerate(serverInfo['node_fw_versions']):
                    serverInfo['about_html'] += str(idx+1) + ":" + str(ver) + " "
                serverInfo['about_html'] += "]"
            serverInfo['about_html'] += "</li>"

        serverInfo['node_api_best'] = NODE_API_BEST
        if serverInfo['node_api_match'] is False or node_api_level < NODE_API_BEST:
            # Show Recommended API notice
            serverInfo['about_html'] += "<li><strong>" + __("Node Update Available") + ": " + str(NODE_API_BEST) + "</strong></li>"

        serverInfo['about_html'] += "</ul>"

        # create version of 'serverInfo' without 'about_html' entry
        serverInfoItems = serverInfo.copy()
        serverInfoItems.pop('about_html', None)
        serverInfoItems['prog_start_epoch'] = "{0:.0f}".format(PROGRAM_START_EPOCH_TIME)
        serverInfoItems['prog_start_time'] = str(datetime.utcfromtimestamp(PROGRAM_START_EPOCH_TIME/1000.0))

        return serverInfo

    except:
        logger.exception("Error in 'buildServerInfo()'")

# Log server/node information
def reportServerInfo():
    logger.debug("Server info:  " + json.dumps(serverInfoItems))
    if serverInfo['node_api_match'] is False:
        logger.info('** WARNING: Node API mismatch **')
        set_ui_message('node-match',
            __("Node versions do not match and may not function similarly"), header='Warning')
    if RACE.num_nodes > 0:
        if serverInfo['node_api_lowest'] < NODE_API_SUPPORTED:
            logger.info('** WARNING: Node firmware is out of date and may not function properly **')
            msgStr = __("Node firmware is out of date and may not function properly")
            if INTERFACE.get_fwupd_serial_name() != None:
                msgStr += ". " + __("If an S32_BPill board is connected, you should") + \
                          " <a href=\"/updatenodes\">" + __("flash-update") + "</a> " + \
                          __("its processor.")
            set_ui_message('node-obs', msgStr, header='Warning', subclass='api-not-supported')
        elif serverInfo['node_api_lowest'] < NODE_API_BEST:
            logger.info('** NOTICE: Node firmware update is available **')
            msgStr = __("Node firmware update is available")
            if INTERFACE.get_fwupd_serial_name() != None:
                msgStr += ". " + __("If an S32_BPill board is connected, you should") + \
                          " <a href=\"/updatenodes\">" + __("flash-update") + "</a> " + \
                          __("its processor.")
            set_ui_message('node-old', msgStr, header='Notice', subclass='api-low')
        elif serverInfo['node_api_lowest'] > NODE_API_BEST:
            logger.warning('** WARNING: Node firmware is newer than this server version supports **')
            set_ui_message('node-newer',
                __("Node firmware is newer than this server version and may not function properly"),
                header='Warning', subclass='api-high')

#
# Program Initialize
#

logger.info('Release: {0} / Server API: {1} / Latest Node API: {2}'.format(RELEASE_VERSION, SERVER_API, NODE_API_BEST))
logger.debug('Program started at {0:.0f}'.format(PROGRAM_START_EPOCH_TIME))
RHUtils.idAndLogSystemInfo()

if RHUtils.isVersionPython2():
    logger.warning("Python version is obsolete: " + RHUtils.getPythonVersionStr())
    set_ui_message('python',
        (__("Python version") + " (" + RHUtils.getPythonVersionStr() + ") " + \
         __("is obsolete and no longer supported; see") + \
         " <a href=\"docs?d=Software Setup.md#python\">Software Settings</a> " + \
         __("doc for upgrade instructions")),
        header='Warning', subclass='old-python')

determineHostAddress(2)  # attempt to determine IP address, but don't wait too long for it

# load plugins
plugin_modules = []
if os.path.isdir('./plugins'):
    dirs = [f.name for f in os.scandir('./plugins') if f.is_dir()]
    for name in dirs:
        try:
            plugin_module = importlib.import_module('plugins.' + name)
            if plugin_module.__file__:
                plugin_modules.append(plugin_module)
                logger.info('Loaded plugin module {0}'.format(name))
            else:
                logger.warning('Plugin module {0} not imported (unable to load file)'.format(name))
        except ImportError as ex:
            logger.warning('Plugin module {0} not imported (not supported or may require additional dependencies)'.format(name))
            logger.debug(ex)
else:
    logger.warning('No plugins directory found.')

for plugin in plugin_modules:
    if 'initialize' in dir(plugin) and callable(getattr(plugin, 'initialize')):
        plugin.initialize(
            Events=Events,
            __=__,
            RHUI=RHUI,
            SOCKET_IO=SOCKET_IO, # Temporary and not supported. Treat as deprecated.
            )

if (not RHGPIO.isS32BPillBoard()) and Config.GENERAL['FORCE_S32_BPILL_FLAG']:
    RHGPIO.setS32BPillBoardFlag()
    logger.info("Set S32BPillBoardFlag in response to FORCE_S32_BPILL_FLAG in config")

logger.debug("isRPi={}, isRealGPIO={}, isS32BPill={}".format(RHUtils.isSysRaspberryPi(), \
                                        RHGPIO.isRealRPiGPIO(), RHGPIO.isS32BPillBoard()))
if RHUtils.isSysRaspberryPi() and not RHGPIO.isRealRPiGPIO():
    logger.warning("Unable to access real GPIO on Pi; try:  sudo pip install RPi.GPIO")
    set_ui_message(
        'gpio',
        __("Unable to access real GPIO on Pi. Try: <code>sudo pip install RPi.GPIO</code>"),
        header='Warning',
        subclass='no-access'
        )

# log results of module initializations
Config.logInitResultMessage()
Language.logInitResultMessage()

# check if current log file owned by 'root' and change owner to 'pi' user if so
if Current_log_path_name and RHUtils.checkSetFileOwnerPi(Current_log_path_name):
    logger.debug("Changed log file owner from 'root' to 'pi' (file: '{0}')".format(Current_log_path_name))
    RHUtils.checkSetFileOwnerPi(log.LOG_DIR_NAME)  # also make sure 'log' dir not owned by 'root'

logger.info("Using log file: {0}".format(Current_log_path_name))

if RHUtils.isSysRaspberryPi() and RHGPIO.isS32BPillBoard():
    try:
        if Config.GENERAL['SHUTDOWN_BUTTON_GPIOPIN']:
            logger.debug("Configuring shutdown-button handler, pin={}, delayMs={}".format(\
                         Config.GENERAL['SHUTDOWN_BUTTON_GPIOPIN'], \
                         Config.GENERAL['SHUTDOWN_BUTTON_DELAYMS']))
            ShutdownButtonInputHandler = ButtonInputHandler(
                            Config.GENERAL['SHUTDOWN_BUTTON_GPIOPIN'], logger, \
                            shutdown_button_pressed, shutdown_button_released, \
                            shutdown_button_long_press,
                            Config.GENERAL['SHUTDOWN_BUTTON_DELAYMS'], False)
            start_shutdown_button_thread()
    except Exception:
        logger.exception("Error setting up shutdown-button handler")

    logger.debug("Resetting S32_BPill processor")
    s32logger = logging.getLogger("stm32loader")
    stm32loader.set_console_output_fn(s32logger.info)
    stm32loader.reset_to_run()
    stm32loader.set_console_output_fn(None)

hardwareHelpers = {}
for helper in search_modules(suffix='helper'):
    try:
        hardwareHelpers[helper.__name__] = helper.create(Config)
    except Exception as ex:
        logger.warning("Unable to create hardware helper '{0}':  {1}".format(helper.__name__, ex))

initRhResultFlag = initialize_rh_interface()
if not initRhResultFlag:
    log.wait_for_queue_empty()
    sys.exit(1)

if len(sys.argv) > 0:
    if CMDARG_JUMP_TO_BL_STR in sys.argv:
        stop_background_threads()
        jump_to_node_bootloader()
        if CMDARG_FLASH_BPILL_STR in sys.argv:
            bootJumpArgIdx = sys.argv.index(CMDARG_FLASH_BPILL_STR) + 1
            bootJumpPortStr = Config.SERIAL_PORTS[0] if Config.SERIAL_PORTS and \
                                                len(Config.SERIAL_PORTS) > 0 else None
            bootJumpSrcStr = sys.argv[bootJumpArgIdx] if bootJumpArgIdx < len(sys.argv) else None
            if bootJumpSrcStr and bootJumpSrcStr.startswith("--"):  # use next arg as src file (optional)
                bootJumpSrcStr = None                       #  unless arg is switch param
            bootJumpSuccessFlag = stm32loader.flash_file_to_stm32(bootJumpPortStr, bootJumpSrcStr)
            sys.exit(0 if bootJumpSuccessFlag else 1)
        sys.exit(0)
    if CMDARG_VIEW_DB_STR in sys.argv:
        try:
            viewdbArgIdx = sys.argv.index(CMDARG_VIEW_DB_STR) + 1
            RHData.backup_db_file(True)
            logger.info("Loading given database file: {}".format(sys.argv[viewdbArgIdx]))
            restoreDbResultFlag = restore_database_file(sys.argv[viewdbArgIdx])
        except Exception as ex:
            logger.error("Error loading database file: {}".format(ex))
            restoreDbResultFlag = False
        if not restoreDbResultFlag:
            sys.exit(1)

CLUSTER = ClusterNodeSet(Language, Events)
hasMirrors = False
try:
    for sec_idx, secondary_info in enumerate(Config.GENERAL['SECONDARIES']):
        if isinstance(secondary_info, string_types):
            secondary_info = {'address': secondary_info, 'mode': SecondaryNode.SPLIT_MODE}
        if 'address' not in secondary_info:
            raise RuntimeError("Secondary 'address' item not specified")
        # substitute asterisks in given address with values from host IP address
        secondary_info['address'] = RHUtils.substituteAddrWildcards(determineHostAddress, \
                                                                secondary_info['address'])
        if 'timeout' not in secondary_info:
            secondary_info['timeout'] = Config.GENERAL['SECONDARY_TIMEOUT']
        if 'mode' in secondary_info and str(secondary_info['mode']) == SecondaryNode.MIRROR_MODE:
            hasMirrors = True
        elif hasMirrors:
            logger.warning('** Mirror secondaries must be last - ignoring remaining secondary config **')
            set_ui_message(
                'secondary',
                __("Mirror secondaries must be last; ignoring part of secondary configuration"),
                header='Notice',
                subclass='mirror'
                )
            break
        secondary = SecondaryNode(sec_idx, secondary_info, RACE, RHData, \
                          RHUI.emit_split_pass_info, monotonic_to_epoch_millis, \
                          RHUI.emit_cluster_connect_change, RELEASE_VERSION)
        CLUSTER.addSecondary(secondary)
except:
    logger.exception("Error adding secondary to cluster")
    set_ui_message(
        'secondary',
        __('Secondary configuration is invalid.'),
        header='Error',
        subclass='error'
        )

if CLUSTER and CLUSTER.hasRecEventsSecondaries():
    CLUSTER.init_repeater()

if RACE.num_nodes > 0:
    logger.info('Number of nodes found: {0}'.format(RACE.num_nodes))
    # if I2C nodes then only report comm errors if > 1.0%
    if hasattr(INTERFACE.nodes[0], 'i2c_addr'):
        INTERFACE.set_intf_error_report_percent_limit(1.0)

# Delay to get I2C addresses through interface class initialization
gevent.sleep(0.500)

try:
    SENSORS.discover(config=Config.SENSORS, **hardwareHelpers)
except Exception:
    logger.exception("Exception while discovering sensors")

# if no DB file then create it now (before "__()" fn used in 'buildServerInfo()')
db_inited_flag = False
if not os.path.exists(DB_FILE_NAME):
    logger.info("No '{0}' file found; creating initial database".format(DB_FILE_NAME))
    db_init()
    db_inited_flag = True
    RHData.primeCache() # Ready the Options cache

# check if DB file owned by 'root' and change owner to 'pi' user if so
if RHUtils.checkSetFileOwnerPi(DB_FILE_NAME):
    logger.debug("Changed DB-file owner from 'root' to 'pi' (file: '{0}')".format(DB_FILE_NAME))

# check if directories owned by 'root' and change owner to 'pi' user if so
if RHUtils.checkSetFileOwnerPi(DB_BKP_DIR_NAME):
    logger.info("Changed '{0}' dir owner from 'root' to 'pi'".format(DB_BKP_DIR_NAME))
if RHUtils.checkSetFileOwnerPi(log.LOGZIP_DIR_NAME):
    logger.info("Changed '{0}' dir owner from 'root' to 'pi'".format(log.LOGZIP_DIR_NAME))

# collect server info for About panel, etc
buildServerInfo()
reportServerInfo()

# Do data consistency checks
if not db_inited_flag:
    try:
        RHData.primeCache() # Ready the Options cache

        if not RHData.check_integrity():
            RHData.recover_database(DB_FILE_NAME, startup=True)
            clean_results_cache()

    except Exception as ex:
        logger.warning('Clearing all data after recovery failure:  ' + str(ex))
        db_reset()

# Create LED object with appropriate configuration
strip = None
if Config.LED['LED_COUNT'] > 0:
    led_type = os.environ.get('RH_LEDS', 'ws281x')
    # note: any calls to 'RHData.get_option()' need to happen after the DB initialization,
    #       otherwise it causes problems when run with no existing DB file
    led_brightness = RHData.get_optionInt("ledBrightness")
    try:
        ledModule = importlib.import_module(led_type + '_leds')
        strip = ledModule.get_pixel_interface(config=Config.LED, brightness=led_brightness)
    except ImportError:
        # No hardware LED handler, the OpenCV emulation
        try:
            ledModule = importlib.import_module('cv2_leds')
            strip = ledModule.get_pixel_interface(config=Config.LED, brightness=led_brightness)
        except ImportError:
            # No OpenCV emulation, try console output
            try:
                ledModule = importlib.import_module('ANSI_leds')
                strip = ledModule.get_pixel_interface(config=Config.LED, brightness=led_brightness)
            except ImportError:
                ledModule = None
                logger.info('LED: disabled (no modules available)')
else:
    logger.debug('LED: disabled (configured LED_COUNT is <= 0)')
if strip:
    # Initialize the library (must be called once before other functions).
    try:
        strip.begin()
        led_manager = LEDEventManager(Events, strip, RHData, RACE, LAST_RACE, Language, INTERFACE)
        init_LED_effects()
    except:
        logger.exception("Error initializing LED support")
        led_manager = NoLEDManager()
elif CLUSTER and CLUSTER.hasRecEventsSecondaries():
    led_manager = ClusterLEDManager(Events)
    init_LED_effects()
else:
    led_manager = NoLEDManager()

# Initialize internal state with database
# DB session commit needed to prevent 'application context' errors
try:
    init_race_state()
except Exception:
    logger.exception("Exception in 'init_race_state()'")
    log.wait_for_queue_empty()
    sys.exit(1)

# internal secondary race format for LiveTime (needs to be created after initial DB setup)
SECONDARY_RACE_FORMAT = RHRace.RHRaceFormat(name=__("Secondary"),
                         race_mode=1,
                         race_time_sec=0,
                         lap_grace_sec=-1,
                         staging_fixed_tones=0,
                         start_delay_min_ms=1000,
                         start_delay_max_ms=1000,
                         staging_tones=0,
                         number_laps_win=0,
                         win_condition=WinCondition.NONE,
                         team_racing_mode=False,
                         start_behavior=0)

# Import IMDTabler
if os.path.exists(IMDTABLER_JAR_NAME):  # if 'IMDTabler.jar' is available
    try:
        java_ver = subprocess.check_output('java -version', stderr=subprocess.STDOUT, shell=True).decode("utf-8")
        logger.debug('Found installed: ' + java_ver.split('\n')[0].strip())
    except:
        java_ver = None
        logger.info('Unable to find java; for IMDTabler functionality try:')
        logger.info('sudo apt install default-jdk-headless')
    if java_ver:
        try:
            chk_imdtabler_ver = subprocess.check_output( \
                        'java -jar ' + IMDTABLER_JAR_NAME + ' -v', \
                        stderr=subprocess.STDOUT, shell=True).decode("utf-8").rstrip()
            Use_imdtabler_jar_flag = True  # indicate IMDTabler.jar available
            logger.debug('Found installed: ' + chk_imdtabler_ver)
        except Exception:
            logger.exception('Error checking IMDTabler:  ')
else:
    logger.info('IMDTabler lib not found at: ' + IMDTABLER_JAR_NAME)
# VRx Controllers
vrx_manager = VRxControlManager(RHData, Events, RACE, INTERFACE.nodes, Language, legacy_config=Config.VRX_CONTROL)
Events.on(Evt.CLUSTER_JOIN, 'VRx', vrx_manager.kill)

# data exporters
export_manager = DataExportManager(RHData, PageCache, Language, Events)

# heat generators
heatgenerate_manager = HeatGeneratorManager(RHData, Results, PageCache, Language, Events)

gevent.spawn(clock_check_thread_function)  # start thread to monitor system clock

# register endpoints
APP.register_blueprint(json_endpoints.createBlueprint(RHData, Results, RACE, serverInfo))

#register event actions
EventActionsObj = EventActions.initializeEventActions(Events, RHData, RACE, RHUI, Language, logger)

RHUI.late_init(INTERFACE, CLUSTER, led_manager, vrx_manager, export_manager, heatgenerate_manager)

@catchLogExceptionsWrapper
def start(port_val=Config.GENERAL['HTTP_PORT'], argv_arr=None):
    if not RHData.get_option("secret_key"):
        RHData.set_option("secret_key", ''.join(random.choice(string.ascii_letters) for _ in range(50)))

    APP.config['SECRET_KEY'] = RHData.get_option("secret_key")
    logger.info("Running http server at port " + str(port_val))
    init_interface_state(startup=True)
    Events.trigger(Evt.STARTUP, {
        'color': ColorVal.ORANGE,
        'message': 'RotorHazard ' + RELEASE_VERSION
        })

    # handle launch-browser arguments ("... [pagename] [browsercmd]")
    if argv_arr and len(argv_arr) > 0:
        launchbIdx = 0
        if CMDARG_VIEW_DB_STR in argv_arr:
            vArgIdx = argv_arr.index(CMDARG_VIEW_DB_STR) + 1
            if len(argv_arr) > vArgIdx + 1 and (not argv_arr[vArgIdx+1].startswith("--")):
                launchbIdx = vArgIdx         # if 'pagename' arg given then launch browser (below)
        if launchbIdx == 0 and CMDARG_LAUNCH_B_STR in argv_arr:
            launchbIdx = argv_arr.index(CMDARG_LAUNCH_B_STR)
        if launchbIdx > 0:
            if len(argv_arr) > launchbIdx + 1 and (not argv_arr[launchbIdx+1].startswith("--")):
                pageStr = argv_arr[launchbIdx+1]
                if not pageStr.startswith('/'):
                    pageStr = '/' +  pageStr
            else:
                pageStr = None
            cmdStr = argv_arr[launchbIdx+2] if len(argv_arr) > launchbIdx+2 and \
                            (not argv_arr[launchbIdx+2].startswith("--")) else None
            start_background_threads(True)
            PageCache.update_cache()
            gevent.spawn_later(2, RHUtils.launchBrowser, "http://localhost", \
                               port_val, pageStr, cmdStr)

    try:
        # the following fn does not return until the server is shutting down
        SOCKET_IO.run(APP, host='0.0.0.0', port=port_val, debug=True, use_reloader=False)
        logger.info("Server is shutting down")
    except KeyboardInterrupt:
        logger.info("Server terminated by keyboard interrupt")
    except SystemExit:
        logger.info("Server terminated by system exit")
    except Exception:
        logger.exception("Server exception")

    Events.trigger(Evt.SHUTDOWN, {
        'color': ColorVal.RED
        })
    rep_str = INTERFACE.get_intf_error_report_str(True)
    if rep_str:
        logger.log((logging.INFO if INTERFACE.get_intf_total_error_count() else logging.DEBUG), rep_str)
    stop_background_threads()
    log.wait_for_queue_empty()
    gevent.sleep(2)  # allow system shutdown command to run before program exit
    log.close_logging()

# Start HTTP server
if __name__ == '__main__':
    start(argv_arr=sys.argv)