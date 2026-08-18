@echo off
rem ============================================================
rem sirius-bridge deploy script
rem Builds the mod and copies the jar into the HMCL test client
rem (version-isolated instance "1.21.1-Sirius") mods directory.
rem Idempotent: old sirius_bridge jars in mods are removed first.
rem
rem Note: the build itself syncs the frozen tool schemas from
rem ..\sirius-brain\schema into the jar (gradle task syncToolSchemas,
rem wired into processResources) - no manual schema copying needed.
rem
rem Proxy flags match the local dev proxy; remove them if the
rem machine has direct internet access.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "MODS_DIR=..\.minecraft\versions\1.21.1-Sirius\mods"

echo [deploy] Building sirius-bridge (NeoForge %~dp0) ...
call gradlew.bat build --console=plain -Dhttps.proxyHost=localhost -Dhttps.proxyPort=9674 -Dhttp.proxyHost=localhost -Dhttp.proxyPort=9674
if errorlevel 1 (
    echo [deploy] BUILD FAILED - deploy aborted.
    exit /b 1
)

if not exist "%MODS_DIR%" (
    echo [deploy] Mods directory not found, creating: %MODS_DIR%
    mkdir "%MODS_DIR%"
)

rem --- Remove old sirius_bridge jars so exactly one version remains ---
for %%F in ("%MODS_DIR%\sirius_bridge-*.jar") do (
    echo [deploy] Removing old jar: %%~nxF
    del /f /q "%%F"
)

rem --- Copy every non-sources/javadoc jar from build\libs ---
set "DEPLOYED="
for %%F in ("build\libs\sirius_bridge-*.jar") do (
    set "NAME=%%~nxF"
    if "!NAME:-sources=!"=="!NAME!" if "!NAME:-javadoc=!"=="!NAME!" (
        copy /y "%%F" "%MODS_DIR%\" >nul
        set "DEPLOYED=!NAME!"
    )
)

if not defined DEPLOYED (
    echo [deploy] ERROR: no deployable jar found in build\libs.
    exit /b 1
)

echo [deploy] Deployed: %MODS_DIR%\%DEPLOYED%
echo [deploy] Done.
endlocal
