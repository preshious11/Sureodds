"""Private staging relay on loopback; no Telegram/D1/generation or deployment.

Needs a reviewed observed-tournament bindings JSON, dedicated ledger and private
token. A production HTTPS reverse proxy/server is NOT started by this tool.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from wsgiref.simple_server import make_server, WSGIRequestHandler

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from predbot.config import load_config
from predbot.sporty_booking import SportyBooking
from predbot.sporty_booking_service import BookingService,BookingHTTPApplication


class QuietHandler(WSGIRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)  # bound slow headers/body reads on loopback

    def log_message(self,*args):
        pass  # never echo request/auth details; protocol returns fixed status only


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bindings',required=True)
    p.add_argument('--ledger',required=True)
    p.add_argument('--port',type=int,default=8788)
    args=p.parse_args()
    cfg=load_config()
    token=cfg.sportybet_booking_service_token
    if len(token)<32 or not all(33<=ord(c)<=126 for c in token):
        p.error('private service token must be configured (32+ printable ASCII characters, no whitespace)')
    bindings=json.loads(Path(args.bindings).read_text(encoding='utf-8'))
    if not isinstance(bindings,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in bindings.items()):
        p.error('bindings must contain reviewed canonical-to-observed-tournament IDs')
    path=Path(args.ledger).resolve()
    if path==Path(cfg.db_path).resolve(): p.error('ledger must not be the bot database')
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as check:
            tables={r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables!={'booking_claims'}: p.error('existing file must be a dedicated booking ledger')
    db=sqlite3.connect(path,check_same_thread=False)
    service=BookingService(db,lambda:SportyBooking(bindings),enabled=cfg.sportybet_booking_enabled)
    app=BookingHTTPApplication(service,token)
    try:
        with make_server('127.0.0.1',args.port,app,handler_class=QuietHandler) as server:
            print('Private loopback relay ready; no production service or Telegram delivery started.',flush=True)
            server.serve_forever()
    finally: db.close()


if __name__=='__main__': main()
