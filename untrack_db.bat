@echo off
cd /d "C:\Users\Vaicos\Desktop\AI\RestockerLocal"
echo ============================================================
echo  Stopping git from tracking the live database
echo ============================================================
echo.
git rm --cached restocker.db
git rm --cached restocker.db-wal
git rm --cached restocker.db-shm
echo.
echo Committing...
git commit -m "stop tracking live db"
echo.
echo Pushing to GitHub...
git push
echo.
echo ============================================================
echo  DONE. Check the lines above for "master -^> master" (success)
echo  or any error in red. Leave this window open for Claude.
echo ============================================================
pause
