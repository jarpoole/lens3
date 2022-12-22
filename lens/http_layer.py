import zlib
from tornado import gen, httputil

from base_layer import NetLayer
from util import MultiOrderedDict, PipeLayer

def zlib_compress(data, wbits):
    cobj = zlib.compressobj(9, zlib.DEFLATED, wbits)
    return cobj.compress(data) + cobj.flush()

class HTTPLayer(NetLayer):
    NAME = "http"

    ENCODERS = {
        "gzip": lambda x: zlib_compress(x, 16 | zlib.MAX_WBITS),
        "deflate": lambda x: zlib_compress(x, -zlib.MAX_WBITS),
        "zlib": lambda x: zlib_compress(x, zlib.MAX_WBITS),
        "identity": lambda x: x,
    }

    DECODERS = {
        "gzip": lambda x: zlib.decompress(x, 16 | zlib.MAX_WBITS),
        "deflate": lambda x: zlib.decompress(x, -zlib.MAX_WBITS),
        "zlib": lambda x: zlib.decompress(x, zlib.MAX_WBITS),
        "identity": lambda x: x,
    }

    CONN_ID_KEY = "tcp_conn"

    def __init__(self, *args, **kwargs):
        self.ports = kwargs.pop("ports", {})
        self.connections = {}

        super(HTTPLayer, self).__init__(*args, **kwargs)

    @gen.coroutine
    def on_read(self, src, conn, data):
        conn_id = conn[self.CONN_ID_KEY]
        if conn_id not in self.connections:
            dst = self.route(src, conn)
            req = self.request(conn, dst, src)
            req.__next__()
            resp = self.response(conn, src, dst)
            resp.__next__()
            self.connections[conn_id] = {src: req, dst: resp}

        if src in {0, 1}:
            self.connections[conn_id][src].send(data)
        else:
            self.log("Unknown src: {}", src)
            yield self.passthru(src, conn, data)
        #yield self.bubble(src, data, conn)


    def parse_header_line(self, hdict, line):
        line = line.decode('iso8859-1')
        if line[0].isspace():
            # continuation of a multi-line header
            new_part = ' ' + line.lstrip()
            hdict.last_value_append(new_part)
        else:
            name, value = line.split(":", 1)
            hdict.push(name, value.strip())

    def request(self, conn, src, dst):
        keep_alive = True
        req = None
        conn = conn.copy()

        req_line = yield 
        while keep_alive and req_line is not None:
            body = b''
            headers = MultiOrderedDict()
            try:
                req = httputil.parse_request_start_line(req_line.strip().decode('iso8859-1'))
            except httputil.HTTPInputError:
                if req_line != b'':
                    self.log("HTTP Error: Malformed request start line: '{}'", req_line)
                req_line = yield
                continue
            while True:
                header_line = yield
                if header_line is None:
                    break
                if not header_line.strip():
                    break
                self.parse_header_line(headers, header_line.strip())

            if req.version == "HTTP/1.0":
                keep_alive = headers.last("connection", "").lower().strip() == "keep-alive"
            else:
                keep_alive = headers.last("connection", "").lower().strip() != "close"

            if "content-length" in headers:
                try:
                    content_length = int(headers.last("content-length"))
                except ValueError:
                    content_length = None
            else:
                content_length = None

            if req.method != "POST":
                content_length = content_length or 0

            
            if header_line is not None:
                #body += conn["lbl_buffers"][dst]
                #conn["lbl_buffers"][dst] = ""
                conn["lbl_disable"](dst)
                while len(body) < content_length or content_length is None:
                    data = yield
                    if data is None:
                        break
                    body += data

            if "content-encoding" in headers:
                conn["http_decoded"] = False
                encoding = headers.last("content-encoding")
                self.log("req encoding: {}", encoding)
                if encoding in self.DECODERS:
                    try:
                        body = self.DECODERS[encoding](body)
                        conn["http_decoded"] = True
                    except:
                        self.log("Unable to decode content '{}' len={}/{}", encoding, len(body), content_length)
            else:
                conn["http_decoded"] = True

            conn["lbl_enable"](dst)
            conn["http_headers"] = headers
            conn["http_request"] = req
            req_line = yield self.bubble(dst, conn, body)

    def response(self, conn, src, dst):
        keep_alive = True
        resp = None
        conn = conn.copy()

        start_line = yield 
        while keep_alive and start_line is not None:
            body = b''
            headers = MultiOrderedDict()
            try:
                resp = httputil.parse_response_start_line(start_line.strip().decode('iso8859-1'))
            except httputil.HTTPInputError:
                if start_line != b'':
                    self.log("HTTP Error: Malformed response start line: '{}'", start_line)
                start_line = yield
                continue
            while True:
                header_line = yield
                if header_line is None:
                    self.log("HTTP Warning: Terminated early?")
                    return
                if not header_line.strip():
                    break
                self.parse_header_line(headers, header_line.strip())

            if resp.version == "HTTP/1.0":
                keep_alive = headers.last("connection", "").lower().strip() == "keep-alive"
            else:
                keep_alive = headers.last("connection", "").lower().strip() != "close"

            if "content-length" in headers:
                try:
                    content_length = int(headers.last("content-length"))
                except ValueError:
                    content_length = None
            else:
                content_length = None


            if header_line is not None:
                #body += conn["lbl_buffers"][dst]
                #conn["lbl_buffers"][dst] = ""
                conn["lbl_disable"](dst)
                while len(body) < content_length or content_length is None:
                    data = yield
                    if data is None:
                        break
                    body += data

            if "content-encoding" in headers:
                conn["http_decoded"] = False
                encoding = headers.last("content-encoding")
                self.log("req encoding: {}", encoding)
                if encoding in self.DECODERS:
                    try:
                        body = self.DECODERS[encoding](body)
                        conn["http_decoded"] = True
                    except:
                        self.log("Unable to decode content '{}' len={}/{}", encoding, len(body), content_length)
            else:
                # Technically it was decoded with the 'identity' encoding...
                conn["http_decoded"] = True

            conn["lbl_enable"](dst)
            conn["http_headers"] = headers
            conn["http_response"] = resp
            start_line = yield self.bubble(dst, conn, body)

    @gen.coroutine
    def on_close(self, src, conn):
        conn_id = conn[self.CONN_ID_KEY]
        if conn_id in self.connections and src in {0, 1}:
            self.connections[conn_id][src].send(bytes())
        yield self.close_bubble(src, conn)

    @gen.coroutine
    def write(self, dst, conn, data):
        if "http_request" in conn:
            start_line = "{0.method} {0.path} {0.version}\r\n".format(conn["http_request"])
        elif "http_response" in conn:
            start_line = "{0.version} {0.code} {0.reason}\r\n".format(conn["http_response"])
        else:
            raise Exception("No start line for HTTP")

        output = start_line.encode('iso8859-1')
        #yield self.write_back(dst, conn, start_line)

        headers = conn["http_headers"]
        if "content-encoding" in headers and conn["http_decoded"]:
            encoding = headers.last("content-encoding")
            if encoding in self.ENCODERS:
                data = self.ENCODERS[encoding](data)

        if "content-length" in headers:
            headers.set("Content-Length", str(len(data)))

        # Remove caching headers
        headers.remove("if-none-match")
        headers.remove("if-modified-since")
        headers.remove("etag")

        # Try to prevent HTTPS upgrade
        headers.remove("upgrade")

        for key, value in headers:
            multiline_value = value.replace("\n", "\n ")
            line = "{}: {}\r\n".format(key, multiline_value)
            output += line.encode('iso8859-1')
            #yield self.write_back(dst, conn, line)

        self.log(">> {}", output)

        #yield self.write_back(dst, conn, "\r\n")
        #yield self.write_back(dst, conn, data)

        output += b'\r\n'
        output += data
        yield self.write_back(dst, conn, output)
        #yield self.write_back(dst, conn, None)


class ImageFlipLayer(PipeLayer):
    NAME = "image_flip"
    COMMAND = ["convert", "-flip", "-", "-"]

    def match(self, src, header):
        if "http_headers" not in header:
            return False
        return header["http_decoded"] and "image" in header["http_headers"].last("content-type", "")

class XSSInjectorLayer(NetLayer):
    NAME = "xss"
    def match(self, src, header):
        if "http_headers" not in header:
            return False
        return header["http_decoded"] and "javascript" in header["http_headers"].last("content-type", "")

    @gen.coroutine
    def write(self, dst, header, payload):
        output = payload + "\nalert('Code succesfully injected!');\n"
        yield self.write_back(dst, header, output)
