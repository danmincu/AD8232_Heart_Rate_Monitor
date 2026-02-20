"""WiFi + SSE test — bypass WebSocket entirely, use Server-Sent Events."""

import network
import socket
import time
import sys

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect('danix', '12345678')
print("Connecting...")
for _ in range(20):
    if wlan.isconnected():
        break
    time.sleep(1)
if not wlan.isconnected():
    print("FAILED")
    raise SystemExit

ip = wlan.ifconfig()[0]
print("IP:", ip)

HTML = b"""<!DOCTYPE html><html><body>
<h1 id="s">Connecting SSE...</h1>
<pre id="log"></pre>
<script>
var log = document.getElementById('log');
function l(t){ log.textContent += t + '\\n'; }
var es = new EventSource('/events');
es.onopen = function(){ document.getElementById('s').textContent = 'SSE Connected!'; l('OPEN'); };
es.onerror = function(e){ l('ERROR readyState=' + es.readyState); };
es.onmessage = function(e){ l('MSG: ' + e.data); };
</script></body></html>"""

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('0.0.0.0', 80))
srv.listen(2)
print("http://%s/" % ip)

sse_client = None
counter = 0

while True:
    # Send SSE data to connected client
    if sse_client:
        try:
            counter += 1
            msg = "data: {\"n\":%d,\"t\":%d}\n\n" % (counter, time.ticks_ms())
            sse_client.sendall(msg.encode())
            sys.stdout.write(".")
        except:
            print("\nSSE client gone")
            try: sse_client.close()
            except: pass
            sse_client = None

    # Accept new connections
    srv.settimeout(0.5 if sse_client else 5)
    try:
        cl, addr = srv.accept()
    except OSError:
        continue

    cl.settimeout(5)
    try:
        raw = cl.recv(2048)
    except:
        cl.close()
        continue
    if not raw:
        cl.close()
        continue

    req_line = raw[:raw.find(b"\r\n")]
    parts = req_line.split(b" ")
    path = parts[1] if len(parts) >= 2 else b"/"
    print("REQ:", path)

    if path == b"/events":
        # SSE response — just a long-lived HTTP response
        cl.sendall(b"HTTP/1.1 200 OK\r\n"
                   b"Content-Type: text/event-stream\r\n"
                   b"Cache-Control: no-cache\r\n"
                   b"Connection: keep-alive\r\n"
                   b"Access-Control-Allow-Origin: *\r\n\r\n")
        print("  SSE stream started")
        cl.settimeout(0.5)
        sse_client = cl
        counter = 0
    else:
        cl.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n")
        cl.sendall(HTML)
        cl.close()
        print("  HTML sent")
