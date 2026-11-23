"""HTTP serving on the private IPv4 loopback interface."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        host, _ = self.server_address
        if host != '127.0.0.1':
            raise ValueError('Office HTTP listeners require the IPv4 loopback address')
        # HTTPServer normally performs reverse DNS, even for a numeric loopback.
        # A local listener has its canonical address before the resolver is ready.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
