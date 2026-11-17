import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Transaction:
    mail_from: str = ""
    recipients: list[str] = field(default_factory=list)
    data: bytes = b""


@dataclass
class Script:
    extensions: list[str] = field(default_factory=lambda: ["8BITMIME", "SIZE 10000000"])
    auth_reply: str = "235 2.7.0 accepted"
    mail_reply: str = "250 2.1.0 ok"
    recipient_replies: dict[str, str] = field(default_factory=dict)
    data_reply: str = "250 2.0.0 queued"
    drop_at: str = ""
    transactions: list[Transaction] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def reply(self, line: str) -> None:
        self.wfile.write(line.encode("ascii") + b"\r\n")

    def handle(self) -> None:
        script = self.server.script
        if script.drop_at == "connect":
            return
        self.reply("220 loopback ready")
        transaction = Transaction()
        while line := self.rfile.readline():
            command = line.decode("ascii").rstrip("\r\n")
            verb = command.split(" ", 1)[0].upper()
            script.commands.append(verb)
            if script.drop_at == verb.lower():
                return
            if verb == "EHLO":
                lines = ["loopback", *script.extensions]
                for extension in lines[:-1]:
                    self.reply(f"250-{extension}")
                self.reply(f"250 {lines[-1]}")
            elif verb == "HELO":
                self.reply("250 loopback")
            elif verb == "AUTH":
                self.reply(script.auth_reply)
            elif verb == "MAIL":
                transaction = Transaction(mail_from=command)
                self.reply(script.mail_reply)
            elif verb == "RCPT":
                address = command.split(":", 1)[1].strip().strip("<>")
                transaction.recipients.append(address)
                self.reply(script.recipient_replies.get(address, "250 2.1.5 ok"))
            elif verb == "DATA":
                self.reply("354 end with <CRLF>.<CRLF>")
                if not self.receive(transaction):
                    return
            elif verb == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("250 ok")

    def receive(self, transaction: Transaction) -> bool:
        script = self.server.script
        lines: list[bytes] = []
        while (line := self.rfile.readline()) != b".\r\n":
            if not line:
                return False
            lines.append(line[1:] if line.startswith(b"..") else line)
        transaction.data = b"".join(lines)
        script.transactions.append(transaction)
        if script.drop_at == "data_end":
            return False
        self.reply(script.data_reply)
        return True


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, script: Script) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = script


@contextmanager
def loopback_smtp(script: Script) -> Iterator[int]:
    server = _Server(script)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
