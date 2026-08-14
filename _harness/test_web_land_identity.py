import sys, types, asyncio, os, json
sys.path.insert(0, '/home/claude/build/hotfix')
import Restocker_web as w

# ---- fake Restocker_main / discord objects -----------------------------------
class Role:
    def __init__(s, n): s.name = n
class Perms:
    def __init__(s, admin=False, mg=False): s.administrator=admin; s.manage_guild=mg
class Member:
    def __init__(s, i, roles=(), perms=None): s.id=i; s.roles=[Role(r) for r in roles]; s.guild_permissions=perms or Perms()
class Guild:
    def __init__(s, gid, owner, members): s.id=gid; s.owner_id=owner; s._m={m.id:m for m in members}
    def get_member(s, i): return s._m.get(i)
class Bot:
    def __init__(s, guilds): s._g={g.id:g for g in guilds}
    def get_guild(s, i): return s._g.get(i)

HOME = 954487497411403806
PARTNER = 111111111111111111

VTECH_MGR   = Member(1001, roles=("Manager",))
VTECH_PLAIN = Member(1002, roles=("Member",))
OWNER_DM    = 694299644825698424           # in MANAGER_DM_IDS
PARTNER_ADMIN = 9009                       # admin in the PARTNER guild, unknown at home

home_guild    = Guild(HOME, owner=1, members=[VTECH_MGR, VTECH_PLAIN])
partner_guild = Guild(PARTNER, owner=PARTNER_ADMIN,
                      members=[Member(PARTNER_ADMIN, perms=Perms(admin=True))])

m = types.ModuleType("Restocker_main")
m.MANAGER_DM_IDS=[1203738126850461738, 694299644825698424]
m.MANAGER_ROLE_NAME="Manager"; m.MANAGER_ROLE_ALT="Admin"
m.NETWORK_SHARED_SECRET="s3cret"
m.bot = Bot([home_guild, partner_guild])
m._BOT_LOOP = None
async def run_on_bot_loop(fn,*a,_timeout=20.0,**k): return fn(*a,**k)
m.run_on_bot_loop = run_on_bot_loop
CALLS=[]
def _record_network_land_close(lid, refund=False):
    CALLS.append(("close",lid,refund)); return {"ok":True,"outcome":"sold"}
def _record_network_land_cancel(lid, rid, is_mgr=False):
    CALLS.append(("cancel",lid,rid,is_mgr)); return {"ok":True,"listing_id":lid}
def _network_land_config(updates=None):
    CALLS.append(("config",updates)); return {"commission_pct":5.0}
m._record_network_land_close=_record_network_land_close
m._record_network_land_cancel=_record_network_land_cancel
m._network_land_config=_network_land_config
async def _notify_network_land(lid, note="", res=None): pass
m._notify_network_land=_notify_network_land
sys.modules["Restocker_main"]=m

class Req:
    def __init__(s, body, secret="s3cret"): s._b=body; s.headers={"X-Network-Secret":secret}
    async def json(s): return s._b

def body_of(resp):
    return json.loads(resp.body.decode()), resp.status

FAIL=[]
def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ")+name+(" | "+str(extra) if extra else ""))
    if not cond: FAIL.append(name)

async def main():
    # ---- _land_manager_ok -----------------------------------------------------
    chk("mgr_ok: V Tech Manager role -> True",  w._land_manager_ok(1001) is True)
    chk("mgr_ok: V Tech plain member -> False", w._land_manager_ok(1002) is False)
    chk("mgr_ok: MANAGER_DM_IDS owner -> True", w._land_manager_ok(OWNER_DM) is True)
    chk("mgr_ok: PARTNER guild admin -> False (THE H5 FIX)", w._land_manager_ok(PARTNER_ADMIN) is False)
    chk("mgr_ok: unknown id -> False",  w._land_manager_ok(424242) is False)
    chk("mgr_ok: garbage -> False",     w._land_manager_ok("not-an-id") is False)
    chk("mgr_ok: None -> False",        w._land_manager_ok(None) is False)
    chk("mgr_ok: empty -> False",       w._land_manager_ok("") is False)

    # fail-closed when the bot isn't ready
    saved = m.bot; m.bot = None
    chk("mgr_ok: bot unready -> False (fails CLOSED)", w._land_manager_ok(1001) is False)
    m.bot = saved

    # ---- /close ---------------------------------------------------------------
    CALLS.clear()
    d,st = body_of(await w._handle_network_land_close(Req({"listing_id":412,"refund_bidder":False})))
    chk("close: OLD satellite payload (no identity) -> 400 refuse", st==400 and not d["ok"], d)
    chk("close: ...and NO settlement ran", CALLS==[], CALLS)

    CALLS.clear()
    d,st = body_of(await w._handle_network_land_close(Req(
        {"listing_id":412,"refund_bidder":False,"requester_id":str(PARTNER_ADMIN)})))
    chk("close: partner-guild admin -> 403 refuse", st==403 and not d["ok"], d)
    chk("close: ...and NO settlement ran", CALLS==[], CALLS)

    CALLS.clear()
    d,st = body_of(await w._handle_network_land_close(Req(
        {"listing_id":412,"refund_bidder":False,"requester_id":"1001"})))
    chk("close: V Tech manager -> allowed", st==200 and d["ok"], d)
    chk("close: ...settlement ran once", CALLS==[("close",412,False)], CALLS)

    d,st = body_of(await w._handle_network_land_close(Req({"listing_id":412}, secret="wrong")))
    chk("close: bad shared secret still 401", st==401, d)

    # ---- /cancel --------------------------------------------------------------
    CALLS.clear()
    d,st = body_of(await w._handle_network_land_cancel(Req(
        {"listing_id":7,"requester_id":str(PARTNER_ADMIN),"bidder_id":str(PARTNER_ADMIN),
         "is_manager":True})))
    chk("cancel: client is_manager=True from partner admin -> core sees is_mgr=False",
        CALLS==[("cancel",7,str(PARTNER_ADMIN),False)], CALLS)

    CALLS.clear()
    await w._handle_network_land_cancel(Req(
        {"listing_id":7,"requester_id":"1001","bidder_id":"1001","is_manager":False}))
    chk("cancel: real V Tech manager -> core sees is_mgr=True (even if body said False)",
        CALLS==[("cancel",7,"1001",True)], CALLS)

    CALLS.clear()
    await w._handle_network_land_cancel(Req({"listing_id":7,"requester_id":"1002"}))
    chk("cancel: seller path still reaches core (requester_id-only payload resolves uid)",
        CALLS==[("cancel",7,"1002",False)], CALLS)

    # ---- /config --------------------------------------------------------------
    CALLS.clear()
    d,st = body_of(await w._handle_network_land_config(Req({})))
    chk("config: READ with old payload still works (backwards compatible)", st==200 and d["ok"], d)
    chk("config: read reached core with updates=None", CALLS==[("config",None)], CALLS)

    CALLS.clear()
    d,st = body_of(await w._handle_network_land_config(Req({"updates":{"commission_pct":0}})))
    chk("config: WRITE with no identity -> 400 refuse", st==400 and not d["ok"], d)
    chk("config: ...and commission NOT written", CALLS==[], CALLS)

    CALLS.clear()
    d,st = body_of(await w._handle_network_land_config(Req(
        {"updates":{"commission_pct":100},"requester_id":str(PARTNER_ADMIN)})))
    chk("config: WRITE by partner admin -> 403 refuse", st==403, d)
    chk("config: ...and commission NOT written", CALLS==[], CALLS)

    CALLS.clear()
    d,st = body_of(await w._handle_network_land_config(Req(
        {"updates":{"commission_pct":6},"requester_id":"1001"})))
    chk("config: WRITE by V Tech manager -> allowed", st==200 and d["ok"], d)
    chk("config: ...write reached core", CALLS==[("config",{"commission_pct":6})], CALLS)

    print()
    print("FAILURES:", FAIL if FAIL else "none")
    return 1 if FAIL else 0

sys.exit(asyncio.run(main()))
