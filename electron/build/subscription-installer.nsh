# Subscription builds intentionally have no seed key or OAuth token provisioning.
# Keep the legacy key installer separate for older opt-in builds.
!macro customUnInstall
  Delete "$APPDATA\${APP_PACKAGE_NAME}\runtime_token"
!macroend
