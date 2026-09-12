# DPAPI provisioning plumbing shared by build/installer.nsh (customInstall)
# and the standalone makensis tests (electron/build/nsis-tests). Do not
# duplicate this code: both consumers !include this file and insert the macro.
#
# Macro: PCU_DPAPI_PROVISION_TO <TARGET_DIR> <KEY> <KEY_VERSION>
#
#   DPAPI-protects the UTF-8 bytes of <KEY> with crypt32::CryptProtectData
#   (current user, NO extra entropy, CRYPTPROTECT_UI_FORBIDDEN - exactly the
#   call backend/secret_store.protect makes) and on SUCCESS writes ONE file:
#     <TARGET_DIR>\provision.json = versioned envelope
#       {"schema":1,"keyVersion":<KEY_VERSION>,"blob":"<base64 of the raw
#        CryptProtectData output>","blob_sha256":"<hex sha256 of the DECODED
#        blob bytes>"}
#   (schema 1; consumed and validated by backend/config.py.)
#
#   The UTF-8 byte form is required: backend/secret_store.unprotect decodes
#   the decrypted bytes as UTF-8 (UTF-16LE is only tolerated defensively), so
#   the plaintext passed to DPAPI must be UTF-8 with NO NUL terminator.
#
#   Result: sets the global var $PCU_DPAPI_PROVISION_RESULT to "ok" or
#   "fail". Callers MUST check it (build/installer.nsh retries once and then
#   either warns the user or continues silently - it never Aborts).
#
#   Width-agnostic: NSIS compiles either a Unicode runtime (NSIS_CHAR_SIZE=2;
#   NSIS strings are UTF-16 - this is what electron-builder production builds
#   use, which default to unicode: true, NsisTarget.js isUnicodeEnabled) or
#   an ANSI runtime (NSIS_CHAR_SIZE=1; NSIS strings are single-byte ANSI,
#   e.g. standalone makensis invocations that do not set `Unicode true`).
#   System::Call 't' params hand over strings in the runtime's native width,
#   so the two runtimes need different plumbing. The Unicode path converts
#   UTF-16 to UTF-8 bytes; the ANSI path passes the single-byte string bytes
#   straight through: the seed key is plain ASCII (provider API key or
#   generated alphanumerics; gen-seed-define's escapeNsis only escapes $ and
#   "), and for ASCII the ANSI byte form IS the UTF-8 byte form. Branching
#   on NSIS_CHAR_SIZE makes the macro correct under both runtimes; note
#   electron-builder production installs are Unicode, where the previous
#   conversion already round-tripped correctly.
#
#   The envelope itself is emitted as PURE ASCII BYTES: the JSON literals go
#   through FileWrite (ASCII strings encode identically under both runtimes)
#   and the base64 through FileWriteByte straight from the CryptBinaryToString
#   buffer, so provision.json is byte-identical UTF-8 under both runtimes -
#   the backend reads it with read_text(encoding="utf-8").
#
#   The SHA-256 is computed in memory over the raw DPAPI output bytes (the
#   bytes the base64 decodes back to) via advapi32 CryptoAPI
#   (CryptAcquireContextA(CRYPT_VERIFYCONTEXT, PROV_RSA_AES) / CryptCreateHash
#   (CALG_SHA_256) / CryptHashData / CryptGetHashParam(HP_HASHVAL)), hex-
#   lowercase like Python hashlib.hexdigest. The base64 comes from
#   crypt32::CryptBinaryToStringA with CRYPT_STRING_BASE64|CRYPT_STRING_NOCRLF
#   (buffer sized from its own sizing call, +16 slack). No raw blob file is
#   ever staged on disk; no plugin beyond System is used. All pointer-valued
#   API outputs go through explicit System::Alloc buffers passed as plain
#   pointer params (never dot-output params, which are only reliable for
#   return values and fixed-size struct reads).
#
#   Fails closed AND interruption-atomic: the envelope is staged at a single
#   ".tmp" name and handed to its final name with ONE Rename, so the only
#   atomicity unit is a single rename. Before the rename no final file
#   exists (only ".tmp" leftovers, cleaned by the next attempt); after it the
#   envelope is complete and valid - there is no intermediate state in which
#   a new blob can be paired with old metadata. On any failure the macro
#   deletes the ".tmp" file, so nothing it wrote remains when it returns
#   (matching the contract above); the backend tolerates the empty state and
#   starts without the built-in key. A pre-existing final envelope from an
#   older successful run is only removed right before its replacement rename.
#   Legacy two-file artifacts of older installers (provision.blob and
#   provision.meta.json) are deleted when provisioning SUCCEEDS (upgrade
#   cleanup), so an old mixed pair can never linger next to the new envelope;
#   on failure they are left untouched (the backend then consumes the legacy
#   pair, i.e. the pre-upgrade state).
#
#   Preserves all NSIS registers ($0-$9, $R0-$R9) via System::Store, so the
#   caller may pass paths built from registers (e.g. "$R7\${APP_PACKAGE_NAME}").
#
# Requires: LogicLib (included below; electron-builder's installer.nsi also
# includes it). Uses the System plugin only (advapi32/crypt32/kernel32 are
# called through it). This file is !included at global scope by the standalone
# tests and from inside Section scope by installSection.nsh, hence the /GLOBAL
# Var below (and no Function blocks - NSIS only allows those at global scope).

!ifndef PCU_DPAPI_PROVISION_INCLUDED
!define PCU_DPAPI_PROVISION_INCLUDED

!include "LogicLib.nsh"

Var /GLOBAL PCU_DPAPI_PROVISION_RESULT

!macro PCU_DPAPI_PROVISION_TO TARGET_DIR KEY KEY_VERSION
  System::Store "s"                       # save $0-$9 and $R0-$R9
  StrCpy $PCU_DPAPI_PROVISION_RESULT "fail"
  StrCpy $R3 0
  StrCpy $R4 0

  # UTF-8 bytes of the key, no NUL terminator, into an allocated buffer:
  # $R2 = byte count, $R3 = buffer, $R4 != 0 on success.
  !if ${NSIS_CHAR_SIZE} == 1
    # ANSI runtime: 't' params are single-byte; the ASCII key bytes ARE
    # UTF-8. Measure with lstrlenA (excludes the NUL), copy the bytes.
    System::Call 'Kernel32::lstrlenA(t "${KEY}") i .R2'
    ${If} $R2 > 0
      System::Alloc $R2
      Pop $R3
      System::Call 'Kernel32::RtlMoveMemory(p R3, t "${KEY}", i R2)'
      StrCpy $R4 1
    ${EndIf}
  !else
    # Unicode runtime: UTF-16 (NSIS internal string) -> UTF-8 bytes of the
    # key, no NUL terminator: first ask for the exact required byte count,
    # then convert.
    StrLen $R1 "${KEY}"
    System::Call 'Kernel32::WideCharToMultiByte(i 65001, i 0, t "${KEY}", i R1, p 0, i 0, p 0, p 0) i .R2'
    ${If} $R2 > 0
      System::Alloc $R2
      Pop $R3
      System::Call 'Kernel32::WideCharToMultiByte(i 65001, i 0, t "${KEY}", i R1, p R3, i R2, p 0, p 0) i .R4'
    ${EndIf}
  !endif
  ${If} $R4 != 0
      # CRYPT_DATA_BLOB in: {cbData = UTF-8 byte count, pbData = UTF-8 bytes}.
      # DATA_BLOB members are DWORD + pointer-sized int; "i" writes them.
      System::Alloc 8
      Pop $R5
      System::Call '*$R5(&i4 R2, i R3)'
      # CRYPT_DATA_BLOB out
      System::Alloc 8
      Pop $R6
      # szDataDescr = NULL (backend/secret_store.protect passes None),
      # pOptionalEntropy = NULL, flags = 1 (CRYPTPROTECT_UI_FORBIDDEN).
      System::Call 'Crypt32::CryptProtectData(p R5, p 0, p 0, p 0, p 0, i 1, p R6) i .R8'
      ${If} $R8 != 0
        # Unpack out-blob {cbData, pbData}: $R8 = cbData, $R9 = pbData.
        System::Call '*$R6(&i4 .R8, i .R9)'
      ${EndIf}
      System::Free $R5
      System::Free $R6
      StrCpy $R5 0
      StrCpy $R6 0
      ${If} $R8 != 0
        CreateDirectory "${TARGET_DIR}"
        ${If} ${FileExists} "${TARGET_DIR}"
          # SHA-256 of the raw DPAPI output bytes (what the base64 decodes
          # back to), hex-lowercase like Python hashlib.hexdigest, into $2.
          StrCpy $2 ""
          System::Alloc 4
          Pop $R1
          System::Call 'Advapi32::CryptAcquireContextA(p R1, p 0, p 0, i 24, i -268435456) i .R7'
          ${If} $R7 != 0
            System::Call '*$R1(&i4 .R2)'
            System::Free $R1
            System::Alloc 4
            Pop $R1
            System::Call 'Advapi32::CryptCreateHash(p R2, i 32780, i 0, i 0, p R1) i .R7'
            ${If} $R7 != 0
              System::Call '*$R1(&i4 .R0)'
              System::Free $R1
              System::Call 'Advapi32::CryptHashData(p R0, p R9, i R8, i 0) i .R7'
              ${If} $R7 != 0
                System::Alloc 32
                Pop $R6
                System::Alloc 4
                Pop $R1
                StrCpy $R7 32
                System::Call '*$R1(&i4 R7)'
                System::Call 'Advapi32::CryptGetHashParam(p R0, i 2, p R6, p R1, i 0) i .R7'
                ${If} $R7 != 0
                  StrCpy $3 0
                  ${While} $3 < 32
                    IntOp $R7 $R6 + $3
                    System::Call '*$R7(&i1 .R4)'
                    IntFmt $6 "%02x" $R4
                    StrCpy $2 "$2$6"
                    IntOp $3 $3 + 1
                  ${EndWhile}
                ${EndIf}
                System::Free $R1
                StrCpy $R1 0
                System::Free $R6
                StrCpy $R6 0
              ${EndIf}
              System::Call 'Advapi32::CryptDestroyHash(p R0)'
            ${EndIf}
            System::Call 'Advapi32::CryptReleaseContext(p R2, i 0)'
          ${EndIf}
          ${If} $2 != ""
            # base64 (CRYPT_STRING_BASE64|CRYPT_STRING_NOCRLF, no line
            # breaks) of the raw DPAPI output: size the buffer via the
            # pszString=NULL call (+16 slack), then encode.
            System::Alloc 4
            Pop $R1
            System::Call 'Crypt32::CryptBinaryToStringA(p R9, i R8, i 1073741825, p 0, p R1) i .R7'
            System::Call '*$R1(&i4 .R6)'
            ${If} $R6 > 0
            ${AndIf} $R6 < 65536
                  IntOp $R6 $R6 + 16
                  System::Call '*$R1(&i4 R6)'
                  System::Alloc $R6
                  Pop $R2
                  System::Call 'Kernel32::RtlZeroMemory(p R2, i R6)'
                  System::Call 'Crypt32::CryptBinaryToStringA(p R9, i R8, i 1073741825, p R2, p R1) i .R7'
                  ${If} $R7 != 0
                    # Stage the whole envelope at the ".tmp" name; the final
                    # name is only handed over by the single commit Rename
                    # below. The base64 length comes from lstrlenA on the
                    # NUL-terminated buffer (not from the call's return,
                    # which is not reliable through the System plugin).
                    System::Call 'Kernel32::lstrlenA(p R2) i .R6'
                    ${If} $R6 > 0
                      FileOpen $R0 "${TARGET_DIR}\provision.json.tmp" w
                      ${If} $R0 != ""
                        ClearErrors
                        FileWrite $R0 `{"schema":1,"keyVersion":${KEY_VERSION},"blob":"`
                        StrCpy $3 0
                        ${While} $3 < $R6
                          IntOp $R7 $R2 + $3
                          System::Call '*$R7(&i1 .R4)'
                          FileWriteByte $R0 $R4
                          IntOp $3 $3 + 1
                        ${EndWhile}
                        FileWrite $R0 `","blob_sha256":"$2"}`
                        FileClose $R0
                        ${IfNot} ${Errors}
                        ${AndIf} ${FileExists} "${TARGET_DIR}\provision.json.tmp"
                          # Commit: ONE rename is the whole atomic step. A
                          # stray DIRECTORY holding the final name must
                          # count as failure (Delete cannot remove it and
                          # the Rename into it fails; the read-open below
                          # verifies the file).
                          Delete "${TARGET_DIR}\provision.json"
                          Rename "${TARGET_DIR}\provision.json.tmp" "${TARGET_DIR}\provision.json"
                          FileOpen $R0 "${TARGET_DIR}\provision.json" r
                          ${If} $R0 != ""
                            FileClose $R0
                            StrCpy $PCU_DPAPI_PROVISION_RESULT "ok"
                            # Upgrade cleanup: remove the legacy two-file
                            # artifacts of older installers so no old mixed
                            # pair can linger next to the new envelope.
                            Delete "${TARGET_DIR}\provision.blob"
                            Delete "${TARGET_DIR}\provision.meta.json"
                          ${EndIf}
                        ${EndIf}
                      ${EndIf}
                    ${EndIf}
                  ${EndIf}
                  System::Free $R2
                  StrCpy $R2 0
                ${EndIf}
              ${EndIf}
              System::Free $R1
              StrCpy $R1 0
            ${EndIf}
            System::Call 'Kernel32::LocalFree(p R9) p .R8'
          ${EndIf}
  ${EndIf}
  ${If} $R3 != 0
    System::Free $R3
  ${EndIf}

  # Sweep: no ".tmp" file ever survives this macro - on success the envelope
  # temp was renamed away, on failure it is removed here. The legacy ".tmp"
  # names are swept too (temps are never authoritative). Final files written
  # by a FAILED attempt do not exist (the commit rename never ran), so when
  # this macro returns with "fail" nothing it wrote remains on disk.
  Delete "${TARGET_DIR}\provision.json.tmp"
  Delete "${TARGET_DIR}\provision.blob.tmp"
  Delete "${TARGET_DIR}\provision.meta.json.tmp"

  System::Store "l"                       # restore $0-$9 and $R0-$R9
!macroend

!endif
