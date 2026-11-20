---
name: vetheuil
description: Create, edit, validate and build Xcode/iOS projects without a Mac. Use for any .xcodeproj/.xcproj or iOS app task; builds on GitHub Actions and delivers unsigned IPAs.
---

# Vétheuil

Work on Xcode projects from any session, without a Mac:
- `vt`, a single-file Python tool with no dependencies, edits and validates project files here, changing only the lines an edit touches.
- macOS GitHub Actions runners compile.
- The result is an unsigned IPA the user sideloads.

Commands like `/vetheuil new|inspect|validate|build|ipa` map to the sections below.

## 0. Bootstrap (once per session)

```bash
VT_REF=58fd1016617dcad6244d99defa8b09ac9b6ddf73   # pinned commit of github.com/sytsu/vetheuil
curl -fsSL "https://raw.githubusercontent.com/sytsu/vetheuil/$VT_REF/vt.py" -o /tmp/vt.py
python3 /tmp/vt.py doctor
```

Call it as `python3 /tmp/vt.py …`, because shell aliases don't persist between commands. If raw.githubusercontent.com is unreachable, fetch it with git instead: `git init /tmp/vtsrc && git -C /tmp/vtsrc fetch --depth 1 https://github.com/sytsu/vetheuil $VT_REF && git -C /tmp/vtsrc checkout -q FETCH_HEAD`, then use `/tmp/vtsrc/vt.py`.

`doctor` says what this environment can do. Editing and validating always work. Builds run locally only if `xcodebuild` exists (on a Mac); otherwise they go through GitHub Actions.

## 1. Orient before editing

- Run `python3 /tmp/vt.py inspect [PROJ]`. It gives compact JSON of targets, key settings, build phases, synchronized folders, packages and schemes. Use it rather than reading the raw `project.pbxproj`.
- Run `python3 /tmp/vt.py validate` once first, so you know the existing warnings and don't blame yourself for them.
- Check the format:
  - `project.pbxproj` can be edited with `vt`.
  - `project.xcproj` (the JSON format from Xcode 27.2+) can be inspected and validated, but not yet edited with `vt`. Hand-edit it minimally, keep its formatting, and check it strictly with `xcprojformatter` (see the appendix). These projects need `--runner xcode-27`.

## 2. Editing rules

- **Synchronized folders** (`syncFolders` in inspect): to add or remove source files, just create or delete them in that folder. The project file doesn't change.
- **Group-based targets:** create the file, then run `vt add-file PATH --target T`.
- **Build settings:** use `vt set KEY VALUE… [--target T | --project-level] [--config C] [--array]` and `vt unset KEY …`.
- **Swift packages:** use `vt add-package URL PRODUCT [--from X | --exact X | --branch B] --target T`.
- **Shared scheme:** use `vt scheme --target T`. CI needs one.
- **Info.plist, `.entitlements`, asset catalog `Contents.json`:** edit as normal files.
- **Never** invent object IDs, rewrite `project.pbxproj` wholesale, or re-save it through another library. Those reorder and reformat hundreds of lines. If `vt` lacks an operation, make the smallest hand edit possible in the surrounding style, then validate.
- Edits re-run `vt validate` automatically. Point out to the user any new or changed shell-script build phase, since those run code at build time.
- Prefer SwiftUI. There's no visual editor for storyboards or xibs here.

## 3. Build (GitHub Actions)

You need a GitHub repo this session can push to. **You can't create repos.** If there isn't one, ask the user to create an empty repo, then attach it and push.

1. **No CI yet?** Run `VT_REF=$VT_REF python3 /tmp/vt.py init-ci [--runner macos-26|xcode-27]`, then commit and push. That writes `.github/workflows/vetheuil.yml`: validate on Linux → unsigned `xcodebuild` on macOS → IPA → a `rolling` pre-release on the default branch. The file is safe to edit.
2. **Watch:** run `gh run list --limit 3`, then `gh run watch <id> --exit-status --interval 20` with a command timeout of about 10 minutes. A small app takes about a minute plus queue time.
3. **On failure:** run `gh run view <id> --log-failed | tail -80`. The job summary also has `vt digest` output. Fix → validate → push → watch again.
4. **gh API limits:** REST works, but GraphQL may be blocked in cloud sessions, which breaks `gh release download`. Use `gh api repos/O/R/releases/tags/rolling --jq '.assets[].browser_download_url'` and curl instead.
5. **Repos you shouldn't add CI to** (forks, upstream): use the shared builder. It needs `sytsu/vetheuil` attached with push access, and it can only build repos its default token can read (public ones).
   ```bash
   gh api -X POST repos/sytsu/vetheuil/actions/workflows/remote-build.yml/dispatches \
     -f ref=main -f 'inputs[repo]=OWNER/NAME' [-f 'inputs[ref]=BRANCH_OR_SHA'] [-f 'inputs[scheme]=S']
   ```
   The IPA lands in the pre-release `build-<run id>` on `sytsu/vetheuil`.

## 4. New app

```bash
python3 /tmp/vt.py new Name com.example.name --display-name "Name" [--ios 17.0]
```
This creates `Name.xcodeproj` (synchronized folder `Name/`, shared scheme, assets, generated icon) plus the SwiftUI sources. Then run `init-ci`, commit and push.

## 5. Deliver

Give the stable URL `https://github.com/OWNER/REPO/releases/download/rolling/<Scheme>.ipa`. The IPA is unsigned: the user signs and installs it with SideStore, AltStore, LiveContainer or similar. Never claim it's installed or running; only the user can check that.

## Limits (state them plainly)

- No SwiftUI previews, Simulator UI, LLDB or Instruments here.
- Apple-only compilers (actool, ibtool, Metal) run only on CI.
- No App Store upload pipeline.
- `.xcproj` needs Xcode 27+.

## Appendix: Swift toolchain (optional)

Only needed for Apple's `xcode-project-format` tools or SwiftPM work. It requires download.swift.org to be reachable. swiftly fails behind some proxies because swift.org serves its signing keys gzip-encoded, so install manually and verify the signature:

```bash
curl -s --compressed https://www.swift.org/keys/all-keys.asc | gpg --batch --import
V=$(curl -s --compressed https://www.swift.org/api/v1/install/releases.json | python3 -c "import json,sys;print(json.load(sys.stdin)[-1]['name'])")
B=https://download.swift.org/swift-$V-release/ubuntu2404/swift-$V-RELEASE/swift-$V-RELEASE-ubuntu24.04.tar.gz
curl -sfL "$B.sig" -o /tmp/swift.sig && curl -sfL "$B" -o /tmp/swift.tgz && gpg --batch --verify /tmp/swift.sig /tmp/swift.tgz
mkdir -p /opt/swift && tar xzf /tmp/swift.tgz -C /opt/swift --strip-components=1   # 1.1 GB download
```

Adjust `ubuntu2404` / `ubuntu24.04` to match the OS. For strict `.xcproj` checks: `git clone --depth 1 https://github.com/apple/xcode-project-format /tmp/xpf && cd /tmp/xpf && /opt/swift/usr/bin/swift build -c release`, then run `.build/release/xcprojformatter PROJ.xcodeproj`. It fails if the file doesn't decode.
