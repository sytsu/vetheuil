#!/usr/bin/env python3
"""vt — Vétheuil: create, edit, validate and build Xcode projects without a Mac.

Single file, standard library only. Edits are surgical: only the lines an edit
touches change, so diffs stay as small as if Xcode had made them.

  vt new NAME BUNDLE_ID          scaffold a SwiftUI app (synchronized folder)
  vt inspect [PROJ]              compact JSON summary of the project
  vt validate [PROJ]             structural checks (exit 1 on errors)
  vt set KEY VALUE…              set a build setting (target/project, configs)
  vt unset KEY                   remove a build setting
  vt add-package URL PRODUCT     add a Swift package product to a target
  vt add-file PATH               add a file to a group-based target
  vt scheme                      write a shared scheme for a target
  vt detect                      find container + scheme for CI (JSON)
  vt init-ci                     write .github/workflows/vetheuil.yml
  vt digest LOG                  condense xcodebuild output to the diagnostics
  vt doctor                      what this environment can do
"""
import argparse
import glob
import hashlib
import json
import os
import plistlib
import random
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zlib

VERSION = "0.1.0"
REPO = "sytsu/vetheuil"
HEX_ID = re.compile(r"^[0-9A-F]{24}$")
SAFE = re.compile(r"^[A-Za-z0-9_$./]+$")
TARGET_ISAS = ("PBXNativeTarget", "PBXAggregateTarget", "PBXLegacyTarget")
T = "\t"


# ───────────────────────────── formatting ──────────────────────────────

def q(value) -> str:
    """Quote a pbxproj string the way Xcode does."""
    value = str(value)
    if value and SAFE.match(value) and not value.startswith("//"):
        return value
    esc = (value.replace("\\", "\\\\").replace('"', '\\"')
           .replace("\n", "\\n").replace("\t", "\\t"))
    return f'"{esc}"'


class Ref:
    """An object ID rendered with Xcode's trailing comment."""

    def __init__(self, oid, comment=None):
        self.oid, self.comment = oid, comment

    def __str__(self):
        return f"{self.oid} /* {self.comment} */" if self.comment else self.oid


def render(v):
    return str(v) if isinstance(v, Ref) else q(v)


def sort_keys(d):
    return sorted(d, key=lambda k: (k != "isa", k))


def fmt_inline(v):
    if isinstance(v, dict):
        return "{" + "".join(f"{q(k)} = {fmt_inline(v[k])}; " for k in sort_keys(v)) + "}"
    if isinstance(v, list):
        return "(" + "".join(f"{fmt_inline(x)}, " for x in v) + ")"
    return render(v)


def fmt_kv(k, v, ind):
    if isinstance(v, dict):
        out = [f"{T*ind}{q(k)} = {{"]
        for kk in sort_keys(v):
            out += fmt_kv(kk, v[kk], ind + 1)
        return out + [f"{T*ind}}};"]
    if isinstance(v, list):
        return [f"{T*ind}{q(k)} = ("] + [f"{T*(ind+1)}{render(x)}," for x in v] + [f"{T*ind});"]
    return [f"{T*ind}{q(k)} = {render(v)};"]


def fmt_object(oid, comment, d, inline=False):
    head = f"{T*2}{Ref(oid, comment)} = "
    if inline:
        return head + fmt_inline(d) + ";\n"
    lines = [head + "{"]
    for k in sort_keys(d):
        lines += fmt_kv(k, d[k], 3)
    return "\n".join(lines + [f"{T*2}}};"]) + "\n"


# ───────────────────────────── parsing ─────────────────────────────────

class ParseError(Exception):
    pass


def skip_ws(text, i):
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise ParseError("unterminated comment")
            i = j + 2
        else:
            break
    return i


WORD = re.compile(r"[^\s;,=(){}\"]+")


def parse_openstep(text):
    """Parse Xcode's OpenStep plist. Returns (value, spans) where spans maps
    each object ID in the `objects` dict to the (start, end) of its entry."""
    spans = {}
    pos = 0

    def value(depth):
        nonlocal pos
        pos = skip_ws(text, pos)
        if pos >= len(text):
            raise ParseError("unexpected end of file")
        c = text[pos]
        if c == "{":
            pos += 1
            d = {}
            while True:
                pos = skip_ws(text, pos)
                if text[pos] == "}":
                    pos += 1
                    return d
                kstart = pos
                k = value(depth + 1)
                pos = skip_ws(text, pos)
                if text[pos] != "=":
                    raise ParseError(f"expected '=' at offset {pos}")
                pos += 1
                d[k] = value(depth + 1)
                pos = skip_ws(text, pos)
                if text[pos] != ";":
                    raise ParseError(f"expected ';' at offset {pos}")
                pos += 1
                if depth == 1:
                    spans[k] = (kstart, pos)
        if c == "(":
            pos += 1
            arr = []
            while True:
                pos = skip_ws(text, pos)
                if text[pos] == ")":
                    pos += 1
                    return arr
                arr.append(value(depth + 1))
                pos = skip_ws(text, pos)
                if text[pos] == ",":
                    pos += 1
                elif text[pos] != ")":
                    raise ParseError(f"expected ',' or ')' at offset {pos}")
        if c == '"':
            pos += 1
            out = []
            while text[pos] != '"':
                if text[pos] == "\\":
                    pos += 1
                    out.append({"n": "\n", "t": "\t"}.get(text[pos], text[pos]))
                else:
                    out.append(text[pos])
                pos += 1
            pos += 1
            return "".join(out)
        m = WORD.match(text, pos)
        if not m:
            raise ParseError(f"unexpected character {c!r} at offset {pos}")
        pos = m.end()
        return m.group(0)

    # the root dict is depth 0, so `objects` is parsed at depth 1 and its entries are recorded
    root = value(0)
    pos = skip_ws(text, pos)
    if pos != len(text):
        raise ParseError(f"trailing content at offset {pos}")
    return root, spans


# ───────────────────────────── project model ───────────────────────────

def find_container(path=None):
    if path:
        return path.rstrip("/")
    cands = sorted(glob.glob("*.xcodeproj")) or sorted(glob.glob("*/*.xcodeproj"))
    if not cands:
        sys.exit("vt: no .xcodeproj found (pass a path)")
    if len(cands) > 1:
        sys.exit("vt: several projects found, pass one: " + ", ".join(cands))
    return cands[0]


class Project:
    def __init__(self, path=None):
        self.path = find_container(path)
        if self.path.endswith("project.pbxproj"):
            self.path = os.path.dirname(self.path)
        self.root = os.path.dirname(os.path.abspath(self.path))
        self.pbx = os.path.join(self.path, "project.pbxproj")
        self.json_path = os.path.join(self.path, "project.xcproj")
        self.format = "pbxproj" if os.path.exists(self.pbx) else (
            "xcproj" if os.path.exists(self.json_path) else None)
        if self.format is None:
            sys.exit(f"vt: {self.path} has neither project.pbxproj nor project.xcproj")
        if self.format == "pbxproj":
            with open(self.pbx, encoding="utf-8") as f:
                self.text = f.read()
            self.original = self.text
            self.reparse()

    def require_pbxproj(self):
        if self.format != "pbxproj":
            sys.exit("vt: editing the JSON .xcproj format isn't supported yet (inspect/validate are)")
        return self

    # parsing / lookup
    def reparse(self):
        self.data, self.spans = parse_openstep(self.text)
        self.objs = self.data.get("objects", {})
        self.parent = {}
        for gid, g in self.objs.items():
            ch = g.get("children")
            if isinstance(ch, list):
                for c in ch:
                    self.parent[c] = gid

    @property
    def project_obj(self):
        return self.objs[self.data["rootObject"]]

    def targets(self):
        return [(t, self.objs[t]) for t in self.project_obj.get("targets", []) if t in self.objs]

    def target(self, name=None, need_app=True):
        ts = self.targets()
        if name:
            for tid, t in ts:
                if t.get("name") == name:
                    return tid, t
            sys.exit(f"vt: no target named {name!r}; targets: " + ", ".join(t.get("name", "?") for _, t in ts))
        apps = [(tid, t) for tid, t in ts if t.get("productType") == "com.apple.product-type.application"]
        pool = apps if need_app and apps else ts
        if len(pool) == 1:
            return pool[0]
        sys.exit("vt: several targets, pass --target: " + ", ".join(t.get("name", "?") for _, t in pool))

    def configs(self, owner_id, config=None):
        lst = self.objs[self.objs[owner_id]["buildConfigurationList"]]
        out = [(c, self.objs[c]) for c in lst.get("buildConfigurations", [])]
        if config:
            out = [(c, o) for c, o in out if o.get("name") == config]
            if not out:
                sys.exit(f"vt: no configuration named {config!r}")
        return out

    def phase(self, target_id, isa):
        for p in self.objs[target_id].get("buildPhases", []):
            if self.objs.get(p, {}).get("isa") == isa:
                return p
        return None

    def disk_path(self, oid):
        parts, cur = [], oid
        while cur in self.objs:
            o = self.objs[cur]
            tree = o.get("sourceTree", "<group>")
            if o.get("path"):
                parts.append(o["path"])
            if tree == "SOURCE_ROOT":
                break
            if tree != "<group>":
                return None
            cur = self.parent.get(cur)
        return os.path.join(self.root, *reversed(parts))

    def name_of(self, oid):
        o = self.objs.get(oid, {})
        return o.get("name") or o.get("path") or o.get("productName") or oid

    def new_id(self):
        while True:
            oid = "%024X" % random.getrandbits(96)
            if oid not in self.objs and oid not in self.text:
                return oid

    # surgical text edits
    def obj_span(self, oid):
        s, e = self.spans[oid]
        s = self.text.rfind("\n", 0, s) + 1
        if self.text[e:e + 1] == "\n":
            e += 1
        return s, e

    def splice(self, start, end, new):
        self.text = self.text[:start] + new + self.text[end:]
        self.reparse()

    def insert_object(self, isa, oid, block):
        begin, end = f"/* Begin {isa} section */\n", f"/* End {isa} section */\n"
        b = self.text.find(begin)
        if b < 0:
            secs = [(m.start(), m.group(1)) for m in
                    re.finditer(r"^/\* Begin (\w+) section \*/\n", self.text, re.M)]
            later = [s for s, nm in secs if nm > isa]
            if later:
                at = later[0]
                self.splice(at, at, begin + block + end + "\n")
            else:
                last = list(re.finditer(r"^/\* End \w+ section \*/\n", self.text, re.M))
                at = last[-1].end() if last else self.text.find("{", self.text.find("objects = {")) + 2
                self.splice(at, at, "\n" + begin + block + end)
            return
        e = self.text.find(end, b)
        at = e
        for m in re.finditer(r"^\t\t(\S+) ", self.text[b + len(begin):e], re.M):
            if m.group(1) > oid:
                at = b + len(begin) + m.start()
                break
        self.splice(at, at, block)

    def add_to_array(self, oid, key, item):
        s, e = self.obj_span(oid)
        blk = self.text[s:e]
        m = re.search(rf"^(\t+){re.escape(key)} = \(\n", blk, re.M)
        if m:
            ind = m.group(1)
            close = blk.find(f"\n{ind});", m.start())
            at = s + close + 1
            self.splice(at, at, f"{ind}\t{item},\n")
            return
        if blk.count("\n") <= 1:
            sys.exit(f"vt: object {oid} is written on one line; can't add {key}")
        self.insert_key(oid, key, fmt_kv(key, [item], 0))

    def insert_key(self, oid, key, lines):
        """Insert key lines (rendered at indent 0) into a multi-line object in sorted position."""
        s, e = self.obj_span(oid)
        blk = self.text[s:e]
        base = len(blk) - len(blk.lstrip("\t"))
        ind = T * (base + 1)
        at = s + blk.rfind(f"{T*base}}};")
        for m in re.finditer(rf"^{ind}(\"?[\w.\-\[\]=*, ]+\"?) = ", blk, re.M):
            k = m.group(1).strip('"')
            if k != "isa" and k > key.strip('"'):
                at = s + m.start()
                break
        self.splice(at, at, "".join(ind + ln + "\n" for ln in lines))

    def settings_block(self, cfg_id):
        s, e = self.obj_span(cfg_id)
        blk = self.text[s:e]
        m = re.search(r"^(\t+)buildSettings = \{\n", blk, re.M)
        if not m:
            sys.exit(f"vt: configuration {cfg_id} has no multi-line buildSettings")
        ind = m.group(1)
        close = blk.find(f"\n{ind}}};", m.end() - 1)
        return s + m.end(), s + close + 1, ind + T

    def set_setting(self, cfg_id, key, value):
        bs, be, ind = self.settings_block(cfg_id)
        new = "".join(ind + ln + "\n" for ln in fmt_kv(key, value, 0)) if value is not None else ""
        blk = self.text[bs:be]
        m = re.search(rf"^{ind}{re.escape(q(key))} = ", blk, re.M)
        if m:
            start = bs + m.start()
            rest = self.text[bs + m.end():be]
            if rest.startswith("(\n"):
                end = bs + m.end() + rest.find(f"\n{ind});") + len(f"\n{ind});") + 1
            else:
                end = bs + m.end() + rest.find(";\n") + 2
            self.splice(start, end, new)
            return "changed" if value is not None else "removed"
        if value is None:
            return "absent"
        at = be
        for km in re.finditer(rf"^{ind}(\"[^\"]+\"|[\w.]+) = ", blk, re.M):
            if km.group(1).strip('"') > key:
                at = bs + km.start()
                break
        self.splice(at, at, new)
        return "added"

    def save(self):
        if self.text == self.original:
            return 0
        parse_openstep(self.text)  # never write something we can't read back
        tmp = self.pbx + ".vt-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self.text)
        os.replace(tmp, self.pbx)
        a, b = self.original.splitlines(), self.text.splitlines()
        return sum(1 for _ in _difflines(a, b))


def _difflines(a, b):
    import difflib
    for ln in difflib.unified_diff(a, b, lineterm="", n=0):
        if ln[:1] in "+-" and not ln.startswith(("+++", "---")):
            yield ln


# ───────────────────────────── commands: read ──────────────────────────

def schemes_for(container):
    out = []
    for sch in sorted(glob.glob(os.path.join(container, "xcshareddata", "xcschemes", "*.xcscheme"))):
        try:
            tree = ET.parse(sch)
        except ET.ParseError:
            continue
        refs = [(br.get("BlueprintName"), br.get("BuildableName")) for br in tree.iter("BuildableReference")]
        out.append({"name": os.path.splitext(os.path.basename(sch))[0], "builds": sorted(set(refs))})
    return out


KEY_SETTINGS = ["PRODUCT_BUNDLE_IDENTIFIER", "PRODUCT_NAME", "IPHONEOS_DEPLOYMENT_TARGET",
                "SDKROOT", "SUPPORTED_PLATFORMS", "INFOPLIST_FILE", "GENERATE_INFOPLIST_FILE",
                "CODE_SIGN_ENTITLEMENTS", "SWIFT_VERSION", "MARKETING_VERSION",
                "CURRENT_PROJECT_VERSION", "TARGETED_DEVICE_FAMILY"]


def cmd_inspect(a):
    p = Project(a.project)
    if p.format == "xcproj":
        d = json.load(open(p.json_path, encoding="utf-8"))
        tg = [t.get("name") for t in d.get("targets", []) if isinstance(t, dict)]
        print(json.dumps({"project": p.path, "format": "xcproj", "keys": sorted(d), "targets": tg}, indent=1))
        return 0
    objs = p.objs
    out = {"project": p.path, "format": "pbxproj", "objectVersion": p.data.get("objectVersion"),
           "configurations": [o.get("name") for _, o in p.configs(p.data["rootObject"])],
           "project_settings": {}, "targets": [], "packages": [], "schemes": schemes_for(p.path)}
    for _, c in p.configs(p.data["rootObject"]):
        for k in KEY_SETTINGS:
            if k in c.get("buildSettings", {}):
                out["project_settings"].setdefault(k, {})[c["name"]] = c["buildSettings"][k]
    for k, per in list(out["project_settings"].items()):
        vals = list(per.values())
        if len(vals) > 1 and all(v == vals[0] for v in vals):
            out["project_settings"][k] = vals[0]
    for pid in p.project_obj.get("packageReferences", []):
        pk = objs.get(pid, {})
        out["packages"].append({"url": pk.get("repositoryURL") or pk.get("relativePath"),
                                "requirement": pk.get("requirement")})
    for tid, t in p.targets():
        info = {"name": t.get("name"), "id": tid, "kind": t.get("isa"),
                "productType": t.get("productType"), "settings": {}, "phases": [],
                "syncFolders": [], "dependencies": [], "packageProducts": []}
        for cid, c in p.configs(tid):
            bs = c.get("buildSettings", {})
            keys = sorted(bs) if a.all_settings else [k for k in KEY_SETTINGS if k in bs]
            for k in keys:
                info["settings"].setdefault(k, {})[c.get("name")] = bs[k]
            if "baseConfigurationReference" in c:
                info["settings"].setdefault("(xcconfig)", {})[c["name"]] = p.name_of(c["baseConfigurationReference"])
        for k, per in info["settings"].items():  # collapse identical per-config values
            vals = list(per.values())
            if len(vals) > 1 and all(v == vals[0] for v in vals):
                info["settings"][k] = vals[0]
        for ph in t.get("buildPhases", []):
            o = objs.get(ph, {})
            ph_info = {"isa": o.get("isa"), "files": len(o.get("files", []))}
            if o.get("name"):
                ph_info["name"] = o["name"]
            info["phases"].append(ph_info)
        for g in t.get("fileSystemSynchronizedGroups", []):
            dp = p.disk_path(g)
            info["syncFolders"].append(os.path.relpath(dp, p.root) if dp else p.name_of(g))
        for d in t.get("dependencies", []):
            info["dependencies"].append(p.name_of(objs.get(d, {}).get("target", d)))
        for d in t.get("packageProductDependencies", []):
            info["packageProducts"].append(objs.get(d, {}).get("productName"))
        out["targets"].append({k: v for k, v in info.items() if v not in ([], {}, None)})
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def cmd_validate(a):
    errors, warnings, notes = [], [], []
    paths = [a.project] if a.project else (sorted(glob.glob("*.xcodeproj")) or sorted(glob.glob("*/*.xcodeproj")))
    if not paths:
        print("no .xcodeproj found")
        return 1
    for path in paths:
        p = Project(path)
        if p.format == "xcproj":
            try:
                json.load(open(p.json_path, encoding="utf-8"))
                notes.append(f"{p.path}: JSON .xcproj parses (run xcprojformatter for a strict check)")
            except json.JSONDecodeError as e:
                errors.append(f"{p.json_path}: JSON error: {e}")
            continue
        objs = p.objs
        if p.data.get("rootObject") not in objs:
            errors.append(f"{p.pbx}: rootObject missing")
            continue
        refs = set()

        def walk(v):
            if isinstance(v, str):
                if HEX_ID.match(v):
                    refs.add(v)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, dict):
                for k, x in v.items():
                    walk(k)
                    walk(x)
        for oid, o in objs.items():
            if "isa" not in o:
                errors.append(f"object {oid} has no isa")
            walk(o)
        for r in sorted(refs - set(objs)):
            errors.append(f"dangling reference {r}")
        tids = {oid for oid, o in objs.items() if o.get("isa") in TARGET_ISAS}
        for oid, o in objs.items():
            isa = o.get("isa")
            if isa == "PBXFileSystemSynchronizedRootGroup":
                dp = p.disk_path(oid)
                if dp and not os.path.isdir(dp):
                    errors.append(f"synchronized folder missing on disk: {os.path.relpath(dp, p.root)}")
            elif isa == "PBXFileReference":
                dp = p.disk_path(oid)
                if dp and not os.path.exists(dp):
                    warnings.append(f"file reference not on disk: {os.path.relpath(dp, p.root)}")
            elif isa == "PBXShellScriptBuildPhase":
                h = hashlib.sha256(o.get("shellScript", "").encode()).hexdigest()[:12]
                warnings.append(f"shell script phase {o.get('name', oid)!r} sha256:{h} — review it")
            elif isa == "PBXBuildFile" and "fileRef" not in o and "productRef" not in o:
                warnings.append(f"build file {oid} has neither fileRef nor productRef")
        for sch in glob.glob(os.path.join(p.path, "xcshareddata", "xcschemes", "*.xcscheme")):
            try:
                tree = ET.parse(sch)
            except ET.ParseError as e:
                errors.append(f"{sch}: XML error: {e}")
                continue
            for br in tree.iter("BuildableReference"):
                if br.get("ReferencedContainer") == f"container:{os.path.basename(p.path)}" \
                        and br.get("BlueprintIdentifier") not in tids:
                    warnings.append(f"{os.path.basename(sch)}: stale reference to missing target "
                                    f"{br.get('BlueprintName')} ({br.get('BlueprintIdentifier')})")
        notes.append(f"{p.path}: {len(objs)} objects, {len(tids)} target(s): "
                     + ", ".join(sorted(objs[t].get("name", "?") for t in tids)))
    for cj in glob.glob("**/*.xcassets/**/Contents.json", recursive=True):
        try:
            d = json.load(open(cj, encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"{cj}: JSON error: {e}")
            continue
        for img in d.get("images", []):
            fn = img.get("filename")
            if fn and not os.path.exists(os.path.join(os.path.dirname(cj), fn)):
                errors.append(f"{cj}: missing image {fn}")
    for pl in glob.glob("**/*.plist", recursive=True) + glob.glob("**/*.entitlements", recursive=True):
        if pl.startswith(("build/", ".build/")) or "/build/" in pl:
            continue
        try:
            with open(pl, "rb") as f:
                plistlib.load(f)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{pl}: plist error: {e}")
    for n in notes:
        print("ok   ", n)
    for w in warnings:
        print("warn ", w)
    for e in errors:
        print("ERROR", e)
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


def after_edit(p, a, what):
    n = p.save()
    print(f"{what} ({n} line(s) changed in {p.pbx})")
    if not getattr(a, "no_validate", False):
        return cmd_validate(argparse.Namespace(project=p.path))
    return 0


# ───────────────────────────── commands: edit ──────────────────────────

def owners(p, a):
    if a.project_level:
        return [p.data["rootObject"]]
    return [p.target(a.target, need_app=False)[0]]


def cmd_set(a):
    p = Project(a.project).require_pbxproj()
    value = a.values if (a.array or len(a.values) > 1) else a.values[0]
    results = []
    for owner in owners(p, a):
        for cid, c in p.configs(owner, a.config):
            results.append(f"{c.get('name')}: {p.set_setting(cid, a.key, value)}")
    return after_edit(p, a, f"{a.key} → {value!r} [{', '.join(results)}]")


def cmd_unset(a):
    p = Project(a.project).require_pbxproj()
    results = []
    for owner in owners(p, a):
        for cid, c in p.configs(owner, a.config):
            results.append(f"{c.get('name')}: {p.set_setting(cid, a.key, None)}")
    return after_edit(p, a, f"{a.key} [{', '.join(results)}]")


def cmd_add_package(a):
    p = Project(a.project).require_pbxproj()
    tid, t = p.target(a.target)
    url = a.url.rstrip("/")
    if a.exact:
        req = {"kind": "exactVersion", "version": a.exact}
    elif a.branch:
        req = {"kind": "branch", "branch": a.branch}
    else:
        req = {"kind": "upToNextMajorVersion", "minimumVersion": a.version or "1.0.0"}
    repo_name = re.sub(r"\.git$", "", url.rsplit("/", 1)[-1])
    pkg_comment = f'XCRemoteSwiftPackageReference "{repo_name}"'
    pkg = next((oid for oid, o in p.objs.items() if o.get("isa") == "XCRemoteSwiftPackageReference"
                and o.get("repositoryURL", "").rstrip("/").removesuffix(".git") == url.removesuffix(".git")), None)
    if not pkg:
        pkg = p.new_id()
        p.insert_object("XCRemoteSwiftPackageReference", pkg, fmt_object(
            pkg, pkg_comment, {"isa": "XCRemoteSwiftPackageReference", "repositoryURL": url, "requirement": req}))
        p.add_to_array(p.data["rootObject"], "packageReferences", Ref(pkg, pkg_comment))
    for d in t.get("packageProductDependencies", []):
        if p.objs.get(d, {}).get("productName") == a.product:
            sys.exit(f"vt: {t.get('name')} already depends on {a.product}")
    prod = p.new_id()
    p.insert_object("XCSwiftPackageProductDependency", prod, fmt_object(
        prod, a.product, {"isa": "XCSwiftPackageProductDependency",
                          "package": Ref(pkg, pkg_comment), "productName": a.product}))
    p.add_to_array(tid, "packageProductDependencies", Ref(prod, a.product))
    fw = p.phase(tid, "PBXFrameworksBuildPhase")
    if not fw:
        fw = p.new_id()
        p.insert_object("PBXFrameworksBuildPhase", fw, fmt_object(fw, "Frameworks", {
            "isa": "PBXFrameworksBuildPhase", "buildActionMask": "2147483647", "files": [],
            "runOnlyForDeploymentPostprocessing": "0"}))
        p.add_to_array(tid, "buildPhases", Ref(fw, "Frameworks"))
    bf = p.new_id()
    p.insert_object("PBXBuildFile", bf, fmt_object(
        bf, f"{a.product} in Frameworks", {"isa": "PBXBuildFile", "productRef": Ref(prod, a.product)}, inline=True))
    p.add_to_array(fw, "files", Ref(bf, f"{a.product} in Frameworks"))
    return after_edit(p, a, f"added {a.product} from {url} to {t.get('name')}")


FILE_TYPES = {".swift": "sourcecode.swift", ".m": "sourcecode.c.objc", ".mm": "sourcecode.cpp.objcpp",
              ".c": "sourcecode.c.c", ".cpp": "sourcecode.cpp.cpp", ".h": "sourcecode.c.h",
              ".metal": "sourcecode.metal", ".plist": "text.plist.xml", ".json": "text.json",
              ".png": "image.png", ".jpg": "image.jpeg", ".xcassets": "folder.assetcatalog",
              ".strings": "text.plist.strings", ".xcstrings": "text.json.xcstrings",
              ".storyboard": "file.storyboard", ".xib": "file.xib", ".entitlements": "text.plist.entitlements",
              ".xcconfig": "text.xcconfig", ".html": "text.html", ".js": "sourcecode.javascript",
              ".css": "text.css", ".ttf": "file", ".otf": "file"}
COMPILED = {".swift", ".m", ".mm", ".c", ".cpp", ".metal"}
NOT_IN_BUILD = {".h", ".plist", ".entitlements", ".xcconfig"}


def cmd_add_file(a):
    p = Project(a.project).require_pbxproj()
    tid, t = p.target(a.target, need_app=False)
    full = os.path.abspath(a.path)
    if not os.path.exists(full):
        sys.exit(f"vt: {a.path} does not exist; create it first")
    for g in t.get("fileSystemSynchronizedGroups", []):
        dp = p.disk_path(g)
        if dp and (full + os.sep).startswith(dp.rstrip(os.sep) + os.sep):
            print(f"{a.path} is inside synchronized folder {os.path.relpath(dp, p.root)} of "
                  f"{t.get('name')}; it is already part of the target. Nothing to do.")
            return 0
    for oid, o in p.objs.items():
        if o.get("isa") == "PBXFileReference" and p.disk_path(oid) == full:
            sys.exit(f"vt: {a.path} is already referenced ({oid})")
    parent_dir = os.path.dirname(full)
    group = None
    for oid, o in p.objs.items():
        if o.get("isa") == "PBXGroup" and p.disk_path(oid) == parent_dir:
            group = oid
            break
    if not group:
        sys.exit(f"vt: no group maps to {os.path.relpath(parent_dir, p.root)}/; add the file to an "
                 "existing group's folder, or convert the group to a folder in Xcode")
    name = os.path.basename(full)
    ext = os.path.splitext(name)[1].lower()
    fr = p.new_id()
    p.insert_object("PBXFileReference", fr, fmt_object(fr, name, {
        "isa": "PBXFileReference", "lastKnownFileType": FILE_TYPES.get(ext, "file"),
        "path": name, "sourceTree": "<group>"}, inline=True))
    p.add_to_array(group, "children", Ref(fr, name))
    msg = f"referenced {a.path} in group {p.name_of(group)}"
    if ext not in NOT_IN_BUILD and not a.no_build:
        isa, label = ("PBXSourcesBuildPhase", "Sources") if ext in COMPILED else ("PBXResourcesBuildPhase", "Resources")
        ph = p.phase(tid, isa)
        if not ph:
            sys.exit(f"vt: target {t.get('name')} has no {label} phase")
        bf = p.new_id()
        p.insert_object("PBXBuildFile", bf, fmt_object(
            bf, f"{name} in {label}", {"isa": "PBXBuildFile", "fileRef": Ref(fr, name)}, inline=True))
        p.add_to_array(ph, "files", Ref(bf, f"{name} in {label}"))
        msg += f", {label} of {t.get('name')}"
    return after_edit(p, a, msg)


# ───────────────────────────── commands: scaffold ──────────────────────

def oid_for(name, role):
    return hashlib.md5(f"{name}:{role}".encode()).hexdigest()[:24].upper()


def scheme_xml(container_name, target_name, product_name, target_id):
    ref = (f'BuildableIdentifier = "primary" BlueprintIdentifier = "{target_id}" '
           f'BuildableName = "{product_name}" BlueprintName = "{target_name}" '
           f'ReferencedContainer = "container:{container_name}"')
    lldb = ('selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB" '
            'selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"')
    runnable = (f'      <BuildableProductRunnable\n         runnableDebuggingMode = "0">\n'
                f'         <BuildableReference {ref}>\n         </BuildableReference>\n'
                f'      </BuildableProductRunnable>\n')
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<Scheme\n   LastUpgradeVersion = "2600"\n'
            f'   version = "1.7">\n   <BuildAction\n      parallelizeBuildables = "YES"\n'
            f'      buildImplicitDependencies = "YES">\n      <BuildActionEntries>\n'
            f'         <BuildActionEntry\n            buildForTesting = "YES"\n'
            f'            buildForRunning = "YES"\n            buildForProfiling = "YES"\n'
            f'            buildForArchiving = "YES"\n            buildForAnalyzing = "YES">\n'
            f'            <BuildableReference {ref}>\n            </BuildableReference>\n'
            f'         </BuildActionEntry>\n      </BuildActionEntries>\n   </BuildAction>\n'
            f'   <TestAction\n      buildConfiguration = "Debug" {lldb}\n'
            f'      shouldUseLaunchSchemeArgsEnv = "YES"\n      shouldAutocreateTestPlan = "YES">\n'
            f'   </TestAction>\n   <LaunchAction\n      buildConfiguration = "Debug" {lldb}\n'
            f'      launchStyle = "0"\n      useCustomWorkingDirectory = "NO"\n'
            f'      ignoresPersistentStateOnLaunch = "NO"\n      debugDocumentVersioning = "YES"\n'
            f'      debugServiceExtension = "internal"\n      allowLocationSimulation = "YES">\n'
            f'{runnable}   </LaunchAction>\n   <ProfileAction\n      buildConfiguration = "Release"\n'
            f'      shouldUseLaunchSchemeArgsEnv = "YES"\n      savedToolIdentifier = ""\n'
            f'      useCustomWorkingDirectory = "NO"\n      debugDocumentVersioning = "YES">\n'
            f'{runnable}   </ProfileAction>\n   <AnalyzeAction\n      buildConfiguration = "Debug">\n'
            f'   </AnalyzeAction>\n   <ArchiveAction\n      buildConfiguration = "Release"\n'
            f'      revealArchiveInOrganizer = "YES">\n   </ArchiveAction>\n</Scheme>\n')


def png_gradient(path, size=1024, top=(78, 128, 150), bottom=(196, 186, 140)):
    """Write an opaque RGB gradient PNG with the standard library only."""
    rows = []
    for y in range(size):
        t = y / (size - 1)
        px = bytes(round(a + (b - a) * t) for a, b in zip(top, bottom))
        rows.append(b"\x00" + px * size)
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def new_pbxproj(name, bundle_id, display, ios):
    I = {r: oid_for(name, r) for r in [
        "product", "syncgroup", "frameworks", "maingroup", "productsgroup", "target", "target_cfglist",
        "sources", "resources", "project", "project_cfglist", "proj_debug", "proj_release",
        "tgt_debug", "tgt_release"]}
    common = {"ALWAYS_SEARCH_USER_PATHS": "NO",
              "ASSETCATALOG_COMPILER_GENERATE_SWIFT_ASSET_SYMBOL_EXTENSIONS": "YES",
              "CLANG_ENABLE_MODULES": "YES", "CLANG_ENABLE_OBJC_ARC": "YES", "COPY_PHASE_STRIP": "NO",
              "ENABLE_STRICT_OBJC_MSGSEND": "YES", "ENABLE_USER_SCRIPT_SANDBOXING": "YES",
              "GCC_C_LANGUAGE_STANDARD": "gnu17", "IPHONEOS_DEPLOYMENT_TARGET": ios,
              "LOCALIZATION_PREFERS_STRING_CATALOGS": "YES", "SDKROOT": "iphoneos"}
    proj_debug = {**common, "DEBUG_INFORMATION_FORMAT": "dwarf", "ENABLE_TESTABILITY": "YES",
                  "GCC_OPTIMIZATION_LEVEL": "0", "GCC_PREPROCESSOR_DEFINITIONS": ["DEBUG=1", "$(inherited)"],
                  "ONLY_ACTIVE_ARCH": "YES", "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG $(inherited)",
                  "SWIFT_OPTIMIZATION_LEVEL": "-Onone"}
    proj_release = {**common, "DEBUG_INFORMATION_FORMAT": "dwarf-with-dsym", "ENABLE_NS_ASSERTIONS": "NO",
                    "SWIFT_COMPILATION_MODE": "wholemodule", "VALIDATE_PRODUCT": "YES"}
    tgt = {"ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon",
           "ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME": "AccentColor", "CODE_SIGN_STYLE": "Automatic",
           "CURRENT_PROJECT_VERSION": "1", "GENERATE_INFOPLIST_FILE": "YES",
           "INFOPLIST_KEY_CFBundleDisplayName": display,
           "INFOPLIST_KEY_UIApplicationSceneManifest_Generation": "YES",
           "INFOPLIST_KEY_UIApplicationSupportsIndirectInputEvents": "YES",
           "INFOPLIST_KEY_UILaunchScreen_Generation": "YES",
           "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPhone": "UIInterfaceOrientationPortrait",
           "LD_RUNPATH_SEARCH_PATHS": ["$(inherited)", "@executable_path/Frameworks"],
           "MARKETING_VERSION": "0.1", "PRODUCT_BUNDLE_IDENTIFIER": bundle_id,
           "PRODUCT_NAME": "$(TARGET_NAME)", "SWIFT_EMIT_LOC_STRINGS": "YES", "SWIFT_VERSION": "6.0",
           "TARGETED_DEVICE_FAMILY": "1"}
    app = f"{name}.app"
    phase = {"buildActionMask": "2147483647", "files": [], "runOnlyForDeploymentPostprocessing": "0"}
    sections = [
        ("PBXFileReference", [fmt_object(I["product"], app, {
            "isa": "PBXFileReference", "explicitFileType": "wrapper.application", "includeInIndex": "0",
            "path": app, "sourceTree": "BUILT_PRODUCTS_DIR"}, inline=True)]),
        ("PBXFileSystemSynchronizedRootGroup", [fmt_object(I["syncgroup"], name, {
            "isa": "PBXFileSystemSynchronizedRootGroup", "path": name, "sourceTree": "<group>"})]),
        ("PBXFrameworksBuildPhase", [fmt_object(I["frameworks"], "Frameworks",
                                                {"isa": "PBXFrameworksBuildPhase", **phase})]),
        ("PBXGroup", [fmt_object(I["maingroup"], None, {
            "isa": "PBXGroup", "children": [Ref(I["syncgroup"], name), Ref(I["productsgroup"], "Products")],
            "sourceTree": "<group>"}),
            fmt_object(I["productsgroup"], "Products", {
                "isa": "PBXGroup", "children": [Ref(I["product"], app)], "name": "Products",
                "sourceTree": "<group>"})]),
        ("PBXNativeTarget", [fmt_object(I["target"], name, {
            "isa": "PBXNativeTarget",
            "buildConfigurationList": Ref(I["target_cfglist"], f'Build configuration list for PBXNativeTarget "{name}"'),
            "buildPhases": [Ref(I["sources"], "Sources"), Ref(I["frameworks"], "Frameworks"),
                            Ref(I["resources"], "Resources")],
            "buildRules": [], "dependencies": [], "fileSystemSynchronizedGroups": [Ref(I["syncgroup"], name)],
            "name": name, "packageProductDependencies": [], "productName": name,
            "productReference": Ref(I["product"], app), "productType": "com.apple.product-type.application"})]),
        ("PBXProject", [fmt_object(I["project"], "Project object", {
            "isa": "PBXProject",
            "attributes": {"BuildIndependentTargetsInParallel": "1", "LastSwiftUpdateCheck": "2600",
                           "LastUpgradeCheck": "2600",
                           "TargetAttributes": {I["target"]: {"CreatedOnToolsVersion": "26.0"}}},
            "buildConfigurationList": Ref(I["project_cfglist"], f'Build configuration list for PBXProject "{name}"'),
            "developmentRegion": "en", "hasScannedForEncodings": "0", "knownRegions": ["en", "Base"],
            "mainGroup": I["maingroup"], "minimizedProjectReferenceProxies": "1",
            "preferredProjectObjectVersion": "77", "productRefGroup": Ref(I["productsgroup"], "Products"),
            "projectDirPath": "", "projectRoot": "", "targets": [Ref(I["target"], name)]})]),
        ("PBXResourcesBuildPhase", [fmt_object(I["resources"], "Resources", {"isa": "PBXResourcesBuildPhase", **phase})]),
        ("PBXSourcesBuildPhase", [fmt_object(I["sources"], "Sources", {"isa": "PBXSourcesBuildPhase", **phase})]),
        ("XCBuildConfiguration", [
            fmt_object(I["proj_debug"], "Debug", {"isa": "XCBuildConfiguration", "buildSettings": proj_debug, "name": "Debug"}),
            fmt_object(I["proj_release"], "Release", {"isa": "XCBuildConfiguration", "buildSettings": proj_release, "name": "Release"}),
            fmt_object(I["tgt_debug"], "Debug", {"isa": "XCBuildConfiguration", "buildSettings": tgt, "name": "Debug"}),
            fmt_object(I["tgt_release"], "Release", {"isa": "XCBuildConfiguration", "buildSettings": tgt, "name": "Release"})]),
        ("XCConfigurationList", [
            fmt_object(I["project_cfglist"], f'Build configuration list for PBXProject "{name}"', {
                "isa": "XCConfigurationList", "buildConfigurations": [Ref(I["proj_debug"], "Debug"), Ref(I["proj_release"], "Release")],
                "defaultConfigurationIsVisible": "0", "defaultConfigurationName": "Release"}),
            fmt_object(I["target_cfglist"], f'Build configuration list for PBXNativeTarget "{name}"', {
                "isa": "XCConfigurationList", "buildConfigurations": [Ref(I["tgt_debug"], "Debug"), Ref(I["tgt_release"], "Release")],
                "defaultConfigurationIsVisible": "0", "defaultConfigurationName": "Release"})]),
    ]
    body = "\n".join(f"/* Begin {isa} section */\n" + "".join(sorted(objs, key=lambda b: b.split()[0]))
                     + f"/* End {isa} section */\n" for isa, objs in sections)
    text = (f"// !$*UTF8*$!\n{{\n{T}archiveVersion = 1;\n{T}classes = {{\n{T}}};\n{T}objectVersion = 77;\n"
            f"{T}objects = {{\n\n{body}{T}}};\n{T}rootObject = {Ref(I['project'], 'Project object')};\n}}\n")
    return text, I


APP_SWIFT = """import SwiftUI

@main
struct @@NAME@@App: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
"""
CONTENT_SWIFT = """import SwiftUI

struct ContentView: View {
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "paintpalette")
                .font(.system(size: 48))
                .foregroundStyle(.tint)
            Text("@@DISPLAY@@")
                .font(.largeTitle.weight(.semibold))
        }
        .padding()
    }
}

#Preview {
    ContentView()
}
"""


def cmd_new(a):
    name = a.name
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name):
        sys.exit("vt: NAME must be an identifier (letters, digits, underscore)")
    out = a.out
    proj = os.path.join(out, f"{name}.xcodeproj")
    if os.path.exists(proj) and not a.force:
        sys.exit(f"vt: {proj} exists (use --force to overwrite the project file only)")
    display = a.display_name or name
    text, I = new_pbxproj(name, a.bundle_id, display, a.ios)
    os.makedirs(os.path.join(proj, "xcshareddata", "xcschemes"), exist_ok=True)
    with open(os.path.join(proj, "project.pbxproj"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(proj, "xcshareddata", "xcschemes", f"{name}.xcscheme"), "w", encoding="utf-8") as f:
        f.write(scheme_xml(f"{name}.xcodeproj", name, f"{name}.app", I["target"]))
    src = os.path.join(out, name)
    assets = os.path.join(src, "Assets.xcassets")
    os.makedirs(os.path.join(assets, "AppIcon.appiconset"), exist_ok=True)
    os.makedirs(os.path.join(assets, "AccentColor.colorset"), exist_ok=True)

    def put(path, content):
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
    put(os.path.join(src, f"{name}App.swift"), APP_SWIFT.replace("@@NAME@@", name))
    put(os.path.join(src, "ContentView.swift"), CONTENT_SWIFT.replace("@@DISPLAY@@", display))
    info = '"info" : {\n    "author" : "xcode",\n    "version" : 1\n  }'
    put(os.path.join(assets, "Contents.json"), "{\n  " + info + "\n}\n")
    put(os.path.join(assets, "AccentColor.colorset", "Contents.json"),
        '{\n  "colors" : [\n    {\n      "idiom" : "universal"\n    }\n  ],\n  ' + info + "\n}\n")
    put(os.path.join(assets, "AppIcon.appiconset", "Contents.json"),
        '{\n  "images" : [\n    {\n      "filename" : "AppIcon.png",\n      "idiom" : "universal",\n'
        '      "platform" : "ios",\n      "size" : "1024x1024"\n    }\n  ],\n  ' + info + "\n}\n")
    icon = os.path.join(assets, "AppIcon.appiconset", "AppIcon.png")
    if not os.path.exists(icon):
        png_gradient(icon)
    print(f"created {proj} and {src}/")
    return cmd_validate(argparse.Namespace(project=proj))


def cmd_scheme(a):
    p = Project(a.project).require_pbxproj()
    tid, t = p.target(a.target)
    prod = p.objs.get(t.get("productReference"), {}).get("path") or f"{t.get('name')}.app"
    sname = a.name or t.get("name")
    d = os.path.join(p.path, "xcshareddata", "xcschemes")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sname}.xcscheme")
    if os.path.exists(path) and not a.force:
        sys.exit(f"vt: {path} exists")
    with open(path, "w", encoding="utf-8") as f:
        f.write(scheme_xml(os.path.basename(p.path), t.get("name"), prod, tid))
    print(f"wrote {path}")
    return 0


# ───────────────────────────── commands: CI ────────────────────────────

def detect(container=None, scheme=None):
    root = os.getcwd()
    if container:
        kind = "workspace" if container.endswith(".xcworkspace") else "project"
    else:
        ws = [w for w in sorted(glob.glob("*.xcworkspace") + glob.glob("*/*.xcworkspace"))
              if ".xcodeproj/" not in w]
        pj = sorted(glob.glob("*.xcodeproj")) or sorted(glob.glob("*/*.xcodeproj"))
        if ws:
            kind, container = "workspace", ws[0]
        elif pj:
            kind, container = "project", pj[0]
        else:
            sys.exit("vt: no .xcodeproj or .xcworkspace found")
    projects = [container] if kind == "project" else sorted(glob.glob("**/*.xcodeproj", recursive=True))
    candidates = []
    for pr in projects:
        for s in schemes_for(pr):
            app = any(b and b.endswith(".app") for _, b in s["builds"])
            candidates.append((not app, s["name"], pr))
    if kind == "workspace":
        for s in schemes_for(container):
            app = any(b and b.endswith(".app") for _, b in s["builds"])
            candidates.append((not app, s["name"], container))
    candidates.sort()
    result = {"kind": kind, "container": container, "scheme": scheme, "scheme_source": "given" if scheme else None}
    if not scheme:
        if candidates:
            result["scheme"], result["scheme_source"] = candidates[0][1], "shared"
        elif kind == "project":
            p = Project(container)
            _, t = p.target()
            result["scheme"], result["scheme_source"] = t.get("name"), "missing (run: vt scheme)"
    result["all_schemes"] = sorted({c[1] for c in candidates})
    os.chdir(root)
    return result


def cmd_detect(a):
    print(json.dumps(detect(a.container, a.scheme), indent=1))
    return 0


WORKFLOW = r"""# Generated by `vt init-ci` (Vétheuil @@VERSION@@). Safe to edit.
name: vetheuil

on:
  push:
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: vetheuil-${{ github.ref }}
  cancel-in-progress: true

env:
  VT_URL: https://raw.githubusercontent.com/@@REPO@@/v@@VERSION@@/vt.py
  KIND: @@KIND@@
  CONTAINER: "@@CONTAINER@@"
  SCHEME: "@@SCHEME@@"
  CONFIGURATION: Release

jobs:
  validate:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v5
      - run: curl -fsSL "$VT_URL" -o /tmp/vt.py && python3 /tmp/vt.py validate @@VALIDATE_ARG@@

  ios:
    needs: validate
    runs-on: @@RUNNER@@
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v5
        with:
          submodules: recursive
      - name: Toolchain
        run: |
          xcodebuild -version
          curl -fsSL "$VT_URL" -o /tmp/vt.py
      - name: Build (unsigned)
        run: |
          set +e
          xcodebuild "-$KIND" "$CONTAINER" -scheme "$SCHEME" -configuration "$CONFIGURATION" \
            -sdk iphoneos -destination 'generic/platform=iOS' -derivedDataPath build \
            CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" \
            build > build.log 2>&1
          status=$?
          python3 /tmp/vt.py digest build.log --markdown >> "$GITHUB_STEP_SUMMARY"
          python3 /tmp/vt.py digest build.log
          exit $status
      - name: Package IPA
        run: |
          APP="$(find "build/Build/Products/$CONFIGURATION-iphoneos" -maxdepth 1 -name '*.app' | head -1)"
          test -n "$APP" || { echo "no .app built"; exit 1; }
          mkdir -p out/Payload && cp -R "$APP" out/Payload/
          (cd out && zip -qry "../$SCHEME.ipa" Payload)
          ls -la "$SCHEME.ipa"
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: build-${{ github.sha }}
          path: |
            build.log
            *.ipa
          if-no-files-found: ignore
      - name: Publish rolling release
        if: github.ref_name == github.event.repository.default_branch
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release delete rolling --yes --cleanup-tag || true
          gh release create rolling "$SCHEME.ipa" --prerelease --target "$GITHUB_SHA" \
            --title "$SCHEME ${GITHUB_SHA::7}" \
            --notes "Unsigned build of ${GITHUB_SHA::7}. Sign and install with SideStore or similar."
"""


def cmd_init_ci(a):
    d = detect(a.container, a.scheme)
    if d["scheme_source"] and d["scheme_source"].startswith("missing"):
        if d["kind"] != "project":
            sys.exit("vt: no shared scheme found; create one in the workspace's project first")
        cmd_scheme(argparse.Namespace(project=d["container"], target=None, name=None, force=False))
    validate_arg = d["container"] if d["kind"] == "project" else ""
    wf = (WORKFLOW.replace("@@VERSION@@", VERSION).replace("@@REPO@@", REPO)
          .replace("@@KIND@@", d["kind"]).replace("@@CONTAINER@@", d["container"])
          .replace("@@SCHEME@@", d["scheme"]).replace("@@RUNNER@@", a.runner)
          .replace("@@VALIDATE_ARG@@", f'"{validate_arg}"' if validate_arg else ""))
    path = os.path.join(".github", "workflows", "vetheuil.yml")
    if os.path.exists(path) and not a.force:
        sys.exit(f"vt: {path} exists (use --force)")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(wf)
    print(f"wrote {path}: {d['kind']} {d['container']}, scheme {d['scheme']}, runner {a.runner}")
    print("push to run it; IPA lands in the 'rolling' release on the default branch")
    return 0


DIAG = re.compile(r"^(?P<loc>(?:/|\.{0,2}/?[\w.@+-]+/)[^:\n]*?:\d+(?::\d+)?): (?P<sev>fatal error|error|warning): (?P<msg>.+)$")
BARE = re.compile(r"^(?:(?:xcodebuild|clang|ld|swift-frontend|actool|ibtool): )?(?P<sev>fatal error|error): (?P<msg>.+)$")
CHECKOUT = re.compile(r"^/Users/runner/work/[^/]+/[^/]+/")


def cmd_digest(a):
    with open(a.log, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    errs, warns, seen, failed, result = [], [], set(), [], None
    in_failed = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("** ") and s.endswith(" **"):
            result = s
        if s.startswith("The following build commands failed:"):
            in_failed = True
            continue
        if in_failed:
            if not s or s.startswith("("):
                in_failed = False
            else:
                failed.append(s)
            continue
        m = DIAG.match(s)
        if m:
            loc = CHECKOUT.sub("", m.group("loc"))
            loc = loc.replace(os.getcwd() + "/", "")
            item = f"{loc}: {m.group('sev')}: {m.group('msg')}"
        else:
            m = BARE.match(s)
            if not m:
                continue
            item = f"{m.group('sev')}: {m.group('msg')}"
        if item in seen:
            continue
        seen.add(item)
        (warns if m.group("sev") == "warning" else errs).append(item)
    out = errs[:a.limit]
    room = max(0, a.limit - len(out))
    out += warns[:room] if not a.errors_only else []
    if len(errs) > a.limit:
        out.append(f"… {len(errs) - a.limit} more error(s)")
    hidden_w = len(warns) - (min(room, len(warns)) if not a.errors_only else 0)
    if hidden_w > 0:
        out.append(f"… {hidden_w} more warning(s)")
    if failed:
        out.append("failed commands: " + " | ".join(failed[:5]))
    out.append(result or "(no ** BUILD … ** line found)")
    text = "\n".join(out)
    if a.markdown:
        print(f"### Vétheuil digest — {len(errs)} error(s), {len(warns)} warning(s)\n```\n{text}\n```")
    else:
        print(text)
    return 1 if errs else 0


def cmd_doctor(a):
    def have(cmd):
        return shutil.which(cmd) is not None

    def reach(url):
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=6)
            return True
        except urllib.error.HTTPError as e:
            return e.code < 500 and e.code != 403
        except Exception:  # noqa: BLE001
            return False

    swift = shutil.which("swift") or ("/opt/swift/usr/bin/swift" if os.path.exists("/opt/swift/usr/bin/swift") else None)
    rows = [
        ("vt", VERSION),
        ("python", sys.version.split()[0]),
        ("platform", sys.platform),
        ("git", have("git")),
        ("gh", have("gh")),
        ("xcodebuild (local builds)", have("xcodebuild")),
        ("swift", swift or False),
        ("github.com", reach("https://github.com")),
        ("raw.githubusercontent.com", reach(f"https://raw.githubusercontent.com/{REPO}/main/README.md")),
        ("download.swift.org", reach("https://download.swift.org/")),
    ]
    for k, v in rows:
        print(f"{k:28} {v}")
    print("\nedit + validate: yes")
    print("build:", "local xcodebuild" if have("xcodebuild") else
          "via GitHub Actions (needs push access to a GitHub repo)")
    return 0


# ───────────────────────────── CLI ─────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(prog="vt", description="Vétheuil: Xcode projects without a Mac")
    ap.add_argument("--version", action="version", version=f"vt {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="scaffold a SwiftUI app")
    s.add_argument("name")
    s.add_argument("bundle_id")
    s.add_argument("--display-name")
    s.add_argument("--ios", default="17.0")
    s.add_argument("--out", default=".")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("inspect", help="JSON summary")
    s.add_argument("project", nargs="?")
    s.add_argument("--all-settings", action="store_true")
    s.set_defaults(fn=cmd_inspect)

    s = sub.add_parser("validate", help="structural checks")
    s.add_argument("project", nargs="?")
    s.set_defaults(fn=cmd_validate)

    for nm, fn in (("set", cmd_set), ("unset", cmd_unset)):
        s = sub.add_parser(nm, help=f"{nm} a build setting")
        s.add_argument("key")
        if nm == "set":
            s.add_argument("values", nargs="+")
            s.add_argument("--array", action="store_true", help="store as an array even with one value")
        s.add_argument("--target")
        s.add_argument("--project-level", action="store_true", help="edit project-level settings")
        s.add_argument("--config", help="only this configuration (default: all)")
        s.add_argument("--project", dest="project")
        s.add_argument("--no-validate", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("add-package", help="add a Swift package product to a target")
    s.add_argument("url")
    s.add_argument("product")
    s.add_argument("--target")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--from", dest="version", help="up to next major from this version (default 1.0.0)")
    g.add_argument("--exact")
    g.add_argument("--branch")
    s.add_argument("--project", dest="project")
    s.add_argument("--no-validate", action="store_true")
    s.set_defaults(fn=cmd_add_package)

    s = sub.add_parser("add-file", help="reference a file in a group-based target")
    s.add_argument("path")
    s.add_argument("--target")
    s.add_argument("--no-build", action="store_true", help="reference only, not in a build phase")
    s.add_argument("--project", dest="project")
    s.add_argument("--no-validate", action="store_true")
    s.set_defaults(fn=cmd_add_file)

    s = sub.add_parser("scheme", help="write a shared scheme")
    s.add_argument("--target")
    s.add_argument("--name")
    s.add_argument("--project", dest="project")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_scheme)

    s = sub.add_parser("detect", help="container + scheme for CI")
    s.add_argument("--container")
    s.add_argument("--scheme")
    s.set_defaults(fn=cmd_detect)

    s = sub.add_parser("init-ci", help="write .github/workflows/vetheuil.yml")
    s.add_argument("--container")
    s.add_argument("--scheme")
    s.add_argument("--runner", default="macos-26", help="e.g. macos-26, xcode-27")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init_ci)

    s = sub.add_parser("digest", help="condense an xcodebuild log")
    s.add_argument("log")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--errors-only", action="store_true")
    s.add_argument("--markdown", action="store_true")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("doctor", help="what this environment can do")
    s.set_defaults(fn=cmd_doctor)

    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
