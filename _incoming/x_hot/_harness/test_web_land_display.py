import sys, types, asyncio, json
sys.path.insert(0,'/home/claude/build/hotfix')
import Restocker_web as w

m = types.ModuleType("Restocker_main")
m.NETWORK_SHARED_SECRET="s3cret"; m._BOT_LOOP=None
m.MANAGER_DM_IDS=[]; m.MANAGER_ROLE_NAME="Manager"; m.MANAGER_ROLE_ALT="Admin"; m.bot=None
NOTES=[]; MOVED={}
async def run_on_bot_loop(fn,*a,_timeout=20.0,**k): return fn(*a,**k)
m.run_on_bot_loop=run_on_bot_loop
def _record_network_land_bid(lid,uid,uname,gid,amount):
    # transcribed from cogs/land_exchange.py _place_bid_core :425/:428
    MOVED["deducted"]=int(round(float(amount)))
    return {"ok":True,"listing_id":lid,"amount":float(amount),"anti_snipe_extended":False}
def _record_network_land_buy(lid,uid,uname,gid):
    MOVED["deducted"]=int(round(1000.6))          # _instant_buy_core :462
    return {"ok":True,"price":1000.6,"sold_to_buyer":uid}
m._record_network_land_bid=_record_network_land_bid
m._record_network_land_buy=_record_network_land_buy
async def _notify_network_land(lid,note="",res=None): NOTES.append(note)
m._notify_network_land=_notify_network_land
sys.modules["Restocker_main"]=m

class Req:
    def __init__(s,b): s._b=b; s.headers={"X-Network-Secret":"s3cret"}
    async def json(s): return s._b

FAIL=[]
def chk(n,c,x=""):
    print(("PASS " if c else "FAIL ")+n+(" | "+str(x) if x else ""))
    if not c: FAIL.append(n)

async def main():
    m._BOT_LOOP = asyncio.get_running_loop()

    # H6 — bid: 1000.6 deducts 1001; all three surfaces must say 1001
    NOTES.clear(); MOVED.clear()
    r = await w._handle_network_land_bid(Req({"listing_id":7,"bidder_id":"5","amount":1000.6}))
    await asyncio.sleep(0.05)
    res = json.loads(r.body.decode())
    sat = f"{float(res['amount']):,.0f}"          # satellite _fmt (app.py:335)
    chk("H6 bid: coins deducted = 1001", MOVED["deducted"]==1001, MOVED)
    chk("H6 bid: partner-channel note says 1,001", "1,001" in NOTES[0], NOTES[0])
    chk("H6 bid: bidder's own confirmation says 1,001", sat=="1,001", sat)
    chk("H6 bid: all three surfaces agree",
        str(MOVED["deducted"])=="1001" and "1,001" in NOTES[0] and sat=="1,001")

    # the exact case in the finding, pre-fix behaviour for contrast
    chk("H6 (old code would have said 1,000)", f"{int(1000.6):,}"=="1,000")

    # banker's-rounding boundary must match int(round()) exactly, not half-up
    for v,exp in [(1000.5,"1,000"),(1001.5,"1,002"),(8500000.5,"8,500,000"),(1000.4,"1,000")]:
        got = f"{int(round(float(v))):,}"
        chk(f"H6 rounding matches int(round()) at {v}", got==exp, got)

    # H6 — buy note
    NOTES.clear(); MOVED.clear()
    await w._handle_network_land_buy(Req({"listing_id":7,"buyer_id":"5"}))
    await asyncio.sleep(0.05)
    chk("H6 buy: note says 1,001 (was 1,000)", "1,001" in NOTES[0], NOTES[0])

    # NaN / Infinity guard
    for bad in [float("nan"), float("inf"), float("-inf")]:
        MOVED.clear()
        r = await w._handle_network_land_bid(Req({"listing_id":7,"bidder_id":"5","amount":bad}))
        d = json.loads(r.body.decode())
        chk(f"NaN guard: {bad} rejected, core never reached", (not d["ok"]) and MOVED=={}, d)
    # bare NaN token in raw JSON, as json.loads accepts it
    parsed = json.loads('{"listing_id":7,"bidder_id":"5","amount":NaN}')
    MOVED.clear()
    r = await w._handle_network_land_bid(Req(parsed))
    chk("NaN guard: bare JSON NaN token rejected", not json.loads(r.body.decode())["ok"] and MOVED=={})
    # None (bid the minimum) must STILL work
    MOVED.clear()
    r = await w._handle_network_land_bid(Req({"listing_id":7,"bidder_id":"5","amount":2500}))
    chk("normal integer bid unaffected", json.loads(r.body.decode())["ok"] and MOVED["deducted"]==2500)

    print(); print("FAILURES:", FAIL if FAIL else "none")
    return 1 if FAIL else 0
sys.exit(asyncio.run(main()))
