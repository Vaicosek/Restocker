# Entry point — forwards to Restocker_main.py so Pterodactyl startup command stays unchanged.
# One-time gear seed (guarded by a bot_config flag inside seed_items, so it runs once).
try:
    import seed_items
    seed_items.main()
except Exception as _seed_err:
    print("[seed] startup hook failed:", _seed_err)
import runpy
runpy.run_path("Restocker_main.py", run_name="__main__")
