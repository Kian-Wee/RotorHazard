import socket

UDP_IP = "192.168.1.224"
UDP_PORT = 21324

# MESSAGE = str(bytearray([0,1,255,255,100,0,0,0,28,5,0,5,0,0,0,100,0,0,0]))
# MESSAGE = str([0,1,255,255,100,0,0,0,28,5,0,5,0,0,0,100,0,0,0])
# MESSAGE = str([0,1,255,255,100,0,0,0,28,5,0,5,0,0,0,100,0,0,0])
MESSAGE = str(bytearray([0,1,255,255,100,0,0,0,28,5,0,5,0,0,0,100,0,0,0]))

print("UDP target IP:", UDP_IP)
print("UDP target port:", UDP_PORT)
print("message:", MESSAGE)

# m=[]

# m.append(1)  # Index of pixel to change
# m.append(p[0][1])  # Pixel red value
# m.append(p[1][1])  # Pixel green value
# m.append(p[2][1])  # Pixel blue value

#   byte udpOut[WLEDPACKETSIZE];
#   Segment& mainseg = strip.getMainSegment();
#   udpOut[0] = 0; //0: wled notifier protocol 1: WARLS protocol
#   udpOut[1] = callMode;
#   udpOut[2] = bri;
#   uint32_t col = mainseg.colors[0];
#   udpOut[3] = R(col);
#   udpOut[4] = G(col);
#   udpOut[5] = B(col);
#   udpOut[6] = nightlightActive;
#   udpOut[7] = nightlightDelayMins;
#   udpOut[8] = mainseg.mode;
#   udpOut[9] = mainseg.speed;
#   udpOut[10] = W(col);

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # UDP
sock.sendto(bytes(MESSAGE, "utf-8"), (UDP_IP, UDP_PORT))