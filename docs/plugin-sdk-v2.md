# Plugin SDK v2

Every plugin uses `manifest.json` with `schemaVersion: 2` and `sdkApi: 2`.
Frontend entries export `createPlugin(host)` and return `routes`, `navigation` and
`dispose()`. Routes must declare `area` and `access` (`public`, `auth`, or `staff`).
Permissions use the explicit Staff roles `reviewer`, `user_manager`, `operator`,
and `administrator`.
