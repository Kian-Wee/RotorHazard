import requests
import time

# curl -X POST "http://[WLED-IP]/json/state" -d '{"on":"t","v":true}' -H "Content-Type: application/json"

UDP_IP = "192.168.1.224"
UDP_PORT = 21324
JSON_IP = 'http://192.168.1.224/json/state'

# r = requests.post('http://192.168.1.224/json/state', json={"key": "value"})

# json = '{"on":"t","v":true}'
# r = requests.post('http://192.168.1.224/json/state', json)
# r.status_code


# json = '{"on":true,"v":true}'
# r = requests.post('http://192.168.1.224/json/state', json)
# r.status_code
# print("on")
# time.sleep(2)

json = '{"on":false,"v":true}'
r = requests.post('http://192.168.1.224/json/state', json)
r.status_code
print("off")
time.sleep(2)

# json = '{"on":true,"v":true,"seg": [{"start": 0,"stop": 300,"len": 300,"col": [[255, 160, 0, 0],[0, 0, 0, 0],[0, 0, 0, 0]],"fx": 0,"sx": 127,"ix": 127,"pal": 0,"sel": true,"rev": false,"cln": -1}}'
# r = requests.post('http://192.168.1.224/json/state', json)
# r.status_code
# print("eff")
# time.sleep(2)

json = '{"on":true,"v":true,"seg": [{"fx":0}]}'
r = requests.post('http://192.168.1.224/json/state', json)
r.status_code
print("eff")
time.sleep(2)

r = requests.post(JSON_IP, '{"on":true,"v":true,"seg":[{"col":[[255, 255, 225]],"bri":255}]}')

# # json = '{"effects":Solid, "palettes":Fire}'
# # r = requests.post('http://192.168.1.224/json/eff', json)
# # r.status_code
# # time.sleep(2)

# # json = '{"effects":["Solid"]}'
# # r = requests.post('http://192.168.1.224/json', json)
# # r.status_code
# # print("solid")
# # time.sleep(2)

# # json = '{"effects":[Palette]}'
# # r = requests.post('http://192.168.1.224/json', json)
# # r.status_code
# # print("palette")
# # time.sleep(2)

# json = '{"on":false,"v":true}'
# r = requests.post('http://192.168.1.224/json/state', json)
# r.status_code
# print("off")
# time.sleep(2)

# json = '{"on":true,"v":true}'
# r = requests.post('http://192.168.1.224/json/state', json)
# r.status_code
# print("on")
# time.sleep(2)

# print(requests.get('http://192.168.1.224/json/eff'))