# Third-party notices

Telos original code is licensed under MIT (LICENSE). Dependencies retain their own licenses.

- `vendor/silk/`: SILK SDK 1.0.9, copyright Skype Limited, BSD-style terms including a no-endorsement condition. Full terms and pinned source provenance are preserved in that directory. Its copyright license explicitly does not grant patent rights. Version 0.4.11 removes the GPL-licensed pilk wrapper; it is not a patent clearance assessment.
- `skills/baidu-ai-map/`: MIT, copyright Baidu Maps Open Platform. See the included LICENSE and UPSTREAM.md.
- Packaged Python and pinned Python dependencies: their license texts are copied into the release `licenses/` directory, with a versioned `dependency-notices.json`. This inventory includes build dependencies; not every listed package is shipped as executable code. PyInstaller's license includes its bootloader distribution exception.
- DSH and its speech model are provided by the user's host installation. Telos does not redistribute the DSH application or speech model.

The obsolete standalone Web UI build is removed from the current tree. Notices for dependencies of that historical UI are retained in `licenses/legacy-web/`. These notices concern Git history, not the current plugin or installer. The current DSH plugin UI source is `plugin/client.js`.
