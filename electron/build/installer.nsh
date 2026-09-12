# Custom NSIS hooks for Personal Computer Use.
#
# Provisioning contract (shared with backend/config.py):
# customInstall DPAPI-protects the bundled seed key with CryptProtectData
# (current user, NO extra entropy - directly interoperable with Python
# CryptUnprotectData(pOptionalEntropy=None)) and writes ONE versioned
# envelope file %APPDATA%\personal-computer-use\provision.json
# {"schema":1,"keyVersion":<int>,"blob":"<base64 DPAPI blob>","blob_sha256":
# "<hex sha256 of the decoded blob>"}. The BACKEND consumes the envelope on
# its first config load (rotation rules), persists the encrypted config and
# deletes it. Electron never decrypts the blob itself. Legacy two-file
# artifacts of older installers (provision.blob + provision.meta.json) are
# removed by the macro when provisioning succeeds.
# Packaged app.asar ships no seed key at all (default-config.json is excluded
# from build.files; seed-define.nsh supplies the key at installer build time).
#
# app-builder-lib's template copies the running installer into the updater
# cache ($LOCALAPPDATA\<app-package-name>-updater, see
# templates/nsis/include/installer.nsh:93) and NSIS forbids redefining its
# macros, so the copy cannot be skipped; customInstall runs right after
# installApplicationFiles (installSection.nsh:81-82) and removes the cached
# copy so no second installer image with the embedded key persists.

# seed-define.nsh is generated at build time (npm run seed-define / predist)
# into electron/build/ and is gitignored: it holds the real seed key. The
# include below resolves because electron-builder adds the buildResourcesDir
# (electron/build) as an !addincludedir for the custom installer include, and
# !include prefers the including file's own directory first (this file lives
# in electron/build next to seed-define.nsh).
#
# NOTE: this file MUST stay the direct target of package.json nsis.include.
# A wrapper include (e.g. in scripts/) that does `!include "installer.nsh"`
# resolves AGAINST electron-builder's template include dir first, which
# ships its own generic installer.nsh - our file would be shadowed and the
# key defines never defined (caught by the guard below during a real build).
!include "seed-define.nsh"

# Staleness guard: a seed-define.nsh written by an older generator (or a
# foreign copy) lacks the schema sentinel. Fail the compile before any
# customInstall macro can embed a wrong or outdated key.
!ifndef PCU_SEED_KEY | PCU_SEED_KEY_VERSION | PCU_SEED_KEY_SCHEMA
  !error "seed-define.nsh is missing expected PCU_SEED_KEY defines (stale or foreign copy): run 'npm run seed-define' or 'npm run dist' (predist regenerates it) and never bypass the npm hooks."
!endif

!include "provision-macro.nsh"

# Provisioning: DPAPI-protect the UTF-8 bytes of the seed key and write the
# single provision.json envelope to %APPDATA%\<app-package-name>. The backend
# consumes and deletes it on first config load. The macro is interruption-
# atomic (one temp write + ONE rename), deletes everything it wrote on
# failure, removes legacy provision.blob/provision.meta.json on success, and
# reports "ok"/"fail" in $PCU_DPAPI_PROVISION_RESULT. When provisioning fails
# even after the in-install retry, customInstall produces an OBSERVABLE
# failure result (see PCU_INSTALL_PROVISION_FLOW below): a distinct nonzero
# process exit code in both the silent and the interactive path, plus a
# durable non-secret marker file on disk. The plumbing used here is proven
# end-to-end (the envelope blob round-trips through
# backend/secret_store.unprotect back to the exact key) by the standalone
# makensis tests in electron/build/nsis-tests (run-provision-tests.ps1).

# Installer process exit code table (set via SetErrorLevel, observable by
# managed/automated callers of e.g. "Setup.exe /S" through the process exit
# code):
#   0 = success - the NSIS default exit level; every path that does NOT hit
#       the provisioning-failure branch below leaves it untouched, so
#       electron-builder's own flow and the uninstaller generation are
#       unaffected.
#   3 = PCU_PROVISION_EXIT_FAILED - provisioning failed after the in-install
#       retry. 1 and 2 are deliberately avoided because NSIS itself uses 1
#       for a user cancel and 2 for exec/general errors, so a managed
#       deployment can distinguish a provisioning failure from those.
!define PCU_PROVISION_EXIT_FAILED 3

# The whole provisioning flow of customInstall, parameterized over the APPDATA
# root so the run-level makensis tests (nsis-tests T10/T11) can insert THIS
# macro - i.e. exercise installer.nsh's actual flow - with a sandbox root,
# without customInstall's real-system touches (%LOCALAPPDATA% updater cache,
# real %APPDATA%).
#
# Behavior: provision, retry once on failure, sweep temps, then:
#   * on SUCCESS: delete a stale <APPDATA_ROOT>\${APP_PACKAGE_NAME}\
#     provisioning-failed.flag if present, so a repaired install never leaves
#     a stale failure marker behind; and
#   * on FAILURE (both attempts failed): write that marker file - a small
#     plaintext file containing ONLY the fixed one-line reason
#     "provisioning failed at install time" (NO key material, nothing derived
#     from the key) - and set the distinct nonzero process exit code
#     PCU_PROVISION_EXIT_FAILED in BOTH the silent and the interactive path
#     (interactive additionally keeps its MessageBox warning). The flag write
#     is best-effort: if even the target dir could not be created (the failure
#     cause), the flag cannot be written either and the exit code is the only
#     signal.
# Do NOT Abort (the app files are already copied and a half-installed state is
# worse than running without the built-in key) and do NOT set the error level
# anywhere else: the normal flow must keep leaving the NSIS default 0.
!macro PCU_INSTALL_PROVISION_FLOW APPDATA_ROOT
  ${If} "${APPDATA_ROOT}" != ""
    !insertmacro PCU_DPAPI_PROVISION_TO "${APPDATA_ROOT}\${APP_PACKAGE_NAME}" "${PCU_SEED_KEY}" "${PCU_SEED_KEY_VERSION}"
    ${If} $PCU_DPAPI_PROVISION_RESULT != "ok"
      # One full retry: a single attempt can lose to a transient file lock
      # (AV scanner, indexer) or a hiccup during the temp writes/renames.
      !insertmacro PCU_DPAPI_PROVISION_TO "${APPDATA_ROOT}\${APP_PACKAGE_NAME}" "${PCU_SEED_KEY}" "${PCU_SEED_KEY_VERSION}"
      ${If} $PCU_DPAPI_PROVISION_RESULT != "ok"
        # The macro already deleted everything it wrote (the envelope temp;
        # the commit rename never ran on failure); sweep once more so no
        # partial file can linger under the target dir. Installing without
        # the key is safe: the backend treats it as "no provisioned key" and
        # starts normally - but the failure must be observable:
        Delete "${APPDATA_ROOT}\${APP_PACKAGE_NAME}\provision.json.tmp"
        Delete "${APPDATA_ROOT}\${APP_PACKAGE_NAME}\provision.blob.tmp"
        Delete "${APPDATA_ROOT}\${APP_PACKAGE_NAME}\provision.meta.json.tmp"
        # Durable non-secret status marker (no key material, fixed reason).
        FileOpen $R6 "${APPDATA_ROOT}\${APP_PACKAGE_NAME}\provisioning-failed.flag" w
        ${If} $R6 != ""
          FileWrite $R6 "provisioning failed at install time$\r$\n"
          FileClose $R6
        ${EndIf}
        # Distinct nonzero exit code in BOTH the silent and the interactive
        # path (MessageBox does not touch the error level).
        SetErrorLevel ${PCU_PROVISION_EXIT_FAILED}
        ${If} ${Silent}
          # Silent installs (managed/automated setups) have nobody to warn;
          # the exit code above and the flag file are the observable result.
        ${Else}
          MessageBox MB_ICONEXCLAMATION "Personal Computer Use could not securely store its built-in key during installation. The app will start without it; reinstalling later may fix this."
        ${EndIf}
      ${EndIf}
    ${EndIf}
    # Clear-on-repair: a successful provisioning (first attempt or retry)
    # removes a stale failure marker from an earlier broken install.
    ${If} $PCU_DPAPI_PROVISION_RESULT == "ok"
      Delete "${APPDATA_ROOT}\${APP_PACKAGE_NAME}\provisioning-failed.flag"
    ${EndIf}
  ${EndIf}
!macroend

!macro customInstall
  # No avoidable installer cache: remove electron-builder's updater-cache copy
  # of this installer (it embeds the seed key too).
  RMDir /r "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"

  ReadEnvStr $R7 APPDATA
  !insertmacro PCU_INSTALL_PROVISION_FLOW "$R7"
!macroend

# Secret-bearing user files are removed on uninstall even though
# deleteAppDataOnUninstall is not enabled.
!macro customUnInstall
  # Reference the provisioning result var so the uninstaller-generation
  # makensis pass (BUILD_UNINSTALLER, where customInstall is never inserted)
  # does not warn 6001 "never referenced" - electron-builder treats warnings
  # as errors. Harmless reset on a var the uninstall flow never reads.
  StrCpy $PCU_DPAPI_PROVISION_RESULT "uninstall"
  ${if} $installMode == "all"
    SetShellVarContext current
  ${endif}
  Delete "$APPDATA\${APP_PACKAGE_NAME}\provision.json"
  Delete "$APPDATA\${APP_PACKAGE_NAME}\provision.blob"
  Delete "$APPDATA\${APP_PACKAGE_NAME}\provision.meta.json"
  # The provisioning-failure marker is non-secret but must not outlive the
  # install either (it would also keep the RMDir below from removing the dir).
  Delete "$APPDATA\${APP_PACKAGE_NAME}\provisioning-failed.flag"
  Delete "$APPDATA\${APP_PACKAGE_NAME}\config.json"
  Delete "$APPDATA\${APP_PACKAGE_NAME}\runtime_token"
  Delete "$APPDATA\${APP_PACKAGE_NAME}\config.json.bak"
  RMDir "$APPDATA\${APP_PACKAGE_NAME}"
  !ifdef APP_PRODUCT_FILENAME
    Delete "$APPDATA\${APP_PRODUCT_FILENAME}\provision.json"
    Delete "$APPDATA\${APP_PRODUCT_FILENAME}\provision.blob"
    Delete "$APPDATA\${APP_PRODUCT_FILENAME}\provision.meta.json"
    Delete "$APPDATA\${APP_PRODUCT_FILENAME}\provisioning-failed.flag"
    Delete "$APPDATA\${APP_PRODUCT_FILENAME}\config.json"
    Delete "$APPDATA\${APP_PRODUCT_FILENAME}\runtime_token"
    Delete "$APPDATA\${APP_PRODUCT_FILENAME}\config.json.bak"
    RMDir "$APPDATA\${APP_PRODUCT_FILENAME}"
  !endif
  ${if} $installMode == "all"
    SetShellVarContext all
  ${endif}
!macroend
