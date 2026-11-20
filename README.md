# Vétheuil

Create, edit, validate and build Xcode projects from any Claude session, with no Mac switched on.

Vétheuil is a Claude skill plus `vt`, a single-file Python tool with no dependencies. Claude edits project files surgically, so only the lines an edit touches change. GitHub Actions' macOS runners do the compiling, and the result is an unsigned IPA you can sideload.

```bash
curl -fsSL https://raw.githubusercontent.com/sytsu/vetheuil/v0.1.0/vt.py -o /tmp/vt.py
python3 /tmp/vt.py doctor
```

| Command | What it does |
|---|---|
| `vt new NAME BUNDLE_ID [--display-name] [--ios 17.0]` | Scaffolds a SwiftUI app: synchronized folder, shared scheme, assets, icon |
| `vt inspect [PROJ] [--all-settings]` | Compact JSON summary: targets, key settings, phases, folders, packages, schemes |
| `vt validate [PROJ]` | Checks references, folders on disk, schemes, asset catalogs and plists, and lists shell-script phases for review |
| `vt set KEY VALUE… [--target T \| --project-level] [--config C]` | Sets a build setting (changes only those lines) |
| `vt unset KEY …` | Removes a build setting |
| `vt add-package URL PRODUCT [--from V \| --exact V \| --branch B] [--target T]` | Adds a Swift package product |
| `vt add-file PATH [--target T]` | References a file in a group-based target (synchronized folders need nothing) |
| `vt scheme [--target T]` | Writes a shared scheme |
| `vt detect` | Finds the container and scheme for CI |
| `vt init-ci [--runner macos-26]` | Writes `.github/workflows/vetheuil.yml`: validate → unsigned build → IPA → `rolling` release |
| `vt digest build.log [--markdown]` | Condenses xcodebuild output to its diagnostics |
| `vt doctor` | Reports what this environment can do |

**Building a repo you can't add CI to:** dispatch `remote-build.yml` in this repo with `repo` (and optionally `ref`, `container`, `scheme`). The IPA lands in a `build-<run id>` pre-release here.

Edits apply to `project.pbxproj`. The JSON `project.xcproj` format (Xcode 27.2+) is detected; `vt` can inspect and validate it but not yet edit it.
