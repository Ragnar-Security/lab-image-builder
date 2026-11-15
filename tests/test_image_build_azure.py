"""The Azure backend of the image builder (scripts/image_build/backends/azure.sh),
driven through build_image.sh against a fake `az` that keeps its own resource
group, VM and gallery state, so ordering (deallocate before the image version)
and the tag re-checks are enforced by the fake and not only asserted on its
log. No Azure account is involved: what only a live run can prove is listed
in scripts/image_build/README.md ("Azure")."""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from tests.test_image_build import (FAKE_BACKENDS, IMAGE_BUILD, KEY_MATERIAL, _build, _build_env, _build_log,
                                    _calls, _fast_sleep, _timeline)

AZURE_BACKEND = IMAGE_BUILD / "backends" / "azure.sh"

SUB = "00000000-0000-4000-8000-000000000001"
OTHER_SUB = "00000000-0000-4000-8000-000000000002"
LOCATION = "eastus"
SUBNET_ID = (f"/subscriptions/{SUB}/resourceGroups/build-net-rg/providers/Microsoft.Network/"
             "virtualNetworks/build-vnet/subnets/build")
GALLERY_RG = "image-gallery-rg"
GALLERY = "build_gallery"
PUBLIC_IP = "192.0.2.30"
PUBKEY = "ssh-ed25519 AAAAC3FAKEPUBLIC image-build"
URN_LINUX = "Canonical:ubuntu-24_04-lts:server:latest"
URN_WINDOWS = "MicrosoftWindowsServer:WindowsServer:2022-datacenter-g2:latest"
LATEST = "24.04.202611010"
RUN_ID_RE = re.compile(r"^ib-[0-9]{12}-[0-9a-f]{16}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
EXPIRED = "202601010000"
UNEXPIRED = "209901010000"
READY = "image-build-ssh-ready"

# A fake az with just the commands the backend uses. Every call is logged to
# $CALL_LOG as `az <shlex-quoted args>`. Values passed as az's @file are read
# from the file: a run-command script is copied to $CALL_LOG.scripts and an
# admin password to $CALL_LOG.secret (test-only: the real az keeps neither).
# State lives in $AZ_STATE (JSON). A missing resource fails as az does: exit 3
# and a "(...NotFound)" code. Anything unrecognised fails loudly.
#
# The resource group is created only through `az rest --method put` on its
# URI. With --debug the fake logs the HTTP status (and a token prefix, as az
# does). An existing group answers 412 to If-None-Match: *, unless
# FAKE_AZ_IGNORE_IF_NONE_MATCH, when the PUT replaces its tags and answers 200.
# FAKE_AZ_RACE: someone else creates the group just before the PUT.
# FAKE_AZ_REST_FAIL_AFTER_APPLY: the PUT is applied, then az exits 1.
# FAKE_AZ_TAG_UPDATE_FAIL=before|after: `tag update` fails without, or after,
# applying the tags.
# FAKE_AZ_RETAG_TAG/_EXPIRES/_OWNER/_CREATING: `vm create` changes that group tag
# (someone retags the build group once the build is under way).
# FAKE_AZ_ERROR="<verb>|<code>|<exit>": that command fails with that code.
# FAKE_AZ_EMPTY="<verb>,...": those commands succeed and print nothing.
# FAKE_AZ_FAULT_SKIP=N: the first N calls matching either fault run normally.
# FAKE_AZ_STRAY_201: another request's "Response status: 201" is logged after
# the PUT's own status. FAKE_AZ_DOUBLE_STATUS: the PUT's status is logged twice.
# FAKE_AZ_REPLACE_AFTER_CREATE: between the 201 and the tag update the group
# is replaced by someone else's of the same name, and the tag update fails.
FAKE_AZ = r'''#!/usr/bin/env python3
import json, os, re, shlex, sys

args = sys.argv[1:]
log = os.environ["CALL_LOG"]
with open(log, "a") as f:
    f.write("az " + shlex.join(args) + "\n")
state_path = os.environ["AZ_STATE"]
state = {"groups": {}, "definitions": {}, "versions": {}}
if os.path.exists(state_path):
    with open(state_path) as f:
        state = json.load(f)


def opt(name):
    if name in args:
        return args[args.index(name) + 1]
    return None


def opts(name):
    if name not in args:
        return []
    out = []
    for a in args[args.index(name) + 1:]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


def fail(msg, rc=1):
    sys.stderr.write(msg + "\n")
    sys.exit(rc)


def not_found(code, msg):
    fail("ERROR: (%s) %s\nCode: %s\nMessage: %s" % (code, msg, code, msg), 3)


def save():
    with open(state_path, "w") as f:
        json.dump(state, f)


def from_file(value, what):
    if not value or not value.startswith("@"):
        fail("fake az: %s must come from a file (@path), got %r" % (what, value), 2)
    with open(value[1:]) as f:
        return f.read()


sub = opt("--subscription")
words = []
for a in args:
    if a.startswith("--") or len(words) == 3:
        break
    words.append(a)
cmd = tuple(words)
verb = " ".join(cmd)
if not sub and cmd != ("rest",):
    fail("fake az: every call needs --subscription", 2)
query = opt("--query")
fail_on = os.environ.get("FAKE_AZ_FAIL", "")
groups = state["groups"]


def faulted(name):
    """True for a call matching an injected fault, after the first
    FAKE_AZ_FAULT_SKIP matching calls (which run normally)."""
    counter = log + ".fault-count"
    seen = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as out:
        out.write(str(seen + 1))
    return seen >= int(os.environ.get("FAKE_AZ_FAULT_SKIP", "0"))


inject = os.environ.get("FAKE_AZ_ERROR", "")
if inject and inject.split("|")[0] == verb and faulted(verb):
    _v, code, rc = inject.split("|")
    fail("ERROR: (%s) injected failure\nCode: %s\nMessage: injected failure" % (code, code), int(rc))
if verb in os.environ.get("FAKE_AZ_EMPTY", "").split(",") and faulted(verb):
    sys.exit(0)


def gallery_key():
    return "/".join([sub, opt("--resource-group"), opt("--gallery-name"), opt("--gallery-image-definition")])


if cmd == ("vm", "image", "show"):
    urn = opt("--urn")
    if not opt("--location"):
        fail("fake az: image show needs --location", 2)
    if urn in os.environ.get("FAKE_AZ_MISSING_URNS", "").split():
        not_found("NotFound", "Artifact: VMImage was not found.")
    publisher, offer, sku, version = urn.split(":")
    if query == "name":
        print(os.environ.get("FAKE_AZ_LATEST", "24.04.202611010") if version == "latest" else version)
    elif query == "[osDiskImage.operatingSystem, hyperVGeneration]":
        print("%s\t%s" % ("Windows" if "Windows" in offer else "Linux", os.environ.get("FAKE_AZ_URN_GEN", "V2")))
    else:
        fail("fake az: unexpected image query " + str(query), 2)
elif cmd == ("sig", "image-definition", "show"):
    d = state["definitions"].get(gallery_key())
    if d is None:
        not_found("ResourceNotFound", "The Resource 'Microsoft.Compute/galleries/%s/images/%s' was not found."
                  % (opt("--gallery-name"), opt("--gallery-image-definition")))
    if query != "[osType, osState, hyperVGeneration]":
        fail("fake az: unexpected definition query " + str(query), 2)
    print("%s\t%s\t%s" % (d["osType"], d["osState"], d["hyperVGeneration"]))
elif cmd == ("sig", "image-definition", "create"):
    key = gallery_key()
    if key in state["definitions"]:
        fail("fake az: definition exists")
    for name in ("--location", "--publisher", "--offer", "--sku", "--os-type", "--os-state", "--hyper-v-generation"):
        if not opt(name):
            fail("fake az: definition create needs " + name, 2)
    state["definitions"][key] = {"osType": opt("--os-type"), "osState": opt("--os-state"),
                                 "hyperVGeneration": opt("--hyper-v-generation"),
                                 "publisher": opt("--publisher"), "offer": opt("--offer"), "sku": opt("--sku")}
    save()
elif cmd == ("sig", "image-version", "show"):
    key = gallery_key() + "/" + opt("--gallery-image-version")
    v = state["versions"].get(key)
    if query == "id" and os.environ.get("FAKE_AZ_VERSION_ID_OUTPUT"):
        print(os.environ["FAKE_AZ_VERSION_ID_OUTPUT"])
        sys.exit(0)
    if v is None and query == "id" and os.environ.get("FAKE_AZ_VERSION_EXISTS"):
        v = {"state": "Succeeded"}
    if v is None:
        not_found("ResourceNotFound", "The Resource 'Microsoft.Compute/galleries/.../versions/%s' was not found."
                  % opt("--gallery-image-version"))
    if query == "provisioningState":
        print(v["state"])
    elif query == "id":
        s, rg, g, d, ver = key.split("/")
        print("/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/galleries/%s/images/%s/versions/%s"
              % (s, rg, g, d, ver))
    else:
        fail("fake az: unexpected version query " + str(query), 2)
elif cmd == ("sig", "image-version", "create"):
    key = gallery_key() + "/" + opt("--gallery-image-version")
    if gallery_key() not in state["definitions"]:
        fail("ERROR: (GalleryImageNotFound) the image definition does not exist")
    if key in state["versions"]:
        fail("fake az: the version exists; az would overwrite it", 2)
    src = opt("--virtual-machine").split("/")
    vm = groups.get(src[4], {}).get("vms", {}).get(src[8])
    if vm is None or src[2] != sub:
        fail("ERROR: (NotFound) source VM " + opt("--virtual-machine"))
    if vm["power"] != "deallocated":
        fail("ERROR: (OperationNotAllowed) the source VM must be deallocated")
    tags = dict(t.split("=", 1) for t in opts("--tags"))
    state["versions"][key] = {"state": "Succeeded", "tags": tags, "source": opt("--virtual-machine"),
                              "location": opt("--location")}
    if fail_on == "version-create":
        state["versions"][key]["state"] = "Failed"
        save()
        fail("ERROR: (InternalExecutionError) An internal execution error occurred.")
    save()
elif cmd == ("group", "show"):
    g = groups.get(opt("--name"))
    if g is None:
        not_found("ResourceGroupNotFound", "Resource group '%s' could not be found." % opt("--name"))
    names = re.fullmatch(r'\[(tags\."[a-z-]+"(, tags\."[a-z-]+")*)\]', query or "")
    if not names:
        fail("fake az: unexpected group query " + str(query), 2)
    values = []
    for key in re.findall(r'tags\."([a-z-]+)"', query):
        value = g["tags"].get(key, "")
        if key == "image-build" and os.environ.get("FAKE_AZ_SHOW_TAG"):
            value = os.environ["FAKE_AZ_SHOW_TAG"]
        if key == "image-build-expires" and os.environ.get("FAKE_AZ_SHOW_EXPIRES"):
            value = os.environ["FAKE_AZ_SHOW_EXPIRES"]
        values.append(value)
    print("\t".join(values))
elif cmd == ("rest",):
    m = re.fullmatch(r"/subscriptions/([^/]+)/resourcegroups/([^/?]+)\?api-version=2021-04-01", opt("--url") or "")
    if opt("--method") != "put" or not m or opt("--headers") != "If-None-Match=*":
        fail("fake az: unexpected rest call", 2)
    sub, name = m.groups()

    def status(code):
        # As az --debug logs it, token prefix included.
        if "--debug" not in args or os.environ.get("FAKE_AZ_REST_NO_STATUS"):
            return
        for _ in range(2 if os.environ.get("FAKE_AZ_DOUBLE_STATUS") else 1):
            sys.stderr.write("cli.azure.cli.core.util: Request URL: 'https://management.azure.com%s'\n"
                             "cli.azure.cli.core.util: Request method: 'PUT'\n"
                             "cli.azure.cli.core.util:     'Authorization': 'Bearer eyJ0eXAiOiJKV1QiLCJh...'\n"
                             "cli.azure.cli.core.util: Response status: %d\n" % (opt("--url"), code))
        if os.environ.get("FAKE_AZ_STRAY_201"):
            # Another request's response, logged after the PUT's.
            sys.stderr.write("cli.azure.cli.core.util: Request URL: 'https://login.microsoftonline.com/x/token'\n"
                             "cli.azure.cli.core.util: Request method: 'POST'\n"
                             "cli.azure.cli.core.util: Response status: 201\n")

    if fail_on == "group-create":
        status(403)
        fail('ERROR: Forbidden({"error":{"code":"AuthorizationFailed","message":"not allowed"}})')
    body = json.loads(opt("--body"))
    if os.environ.get("FAKE_AZ_RACE") and name not in groups:
        groups[name] = {"location": body["location"], "tags": {"owner": "someone-else"},
                        "vms": {"theirs": {"power": "running", "args": []}}}
    tags = dict(body["tags"])
    if name in groups:
        if not os.environ.get("FAKE_AZ_IGNORE_IF_NONE_MATCH"):
            save()
            status(412)
            fail('ERROR: Precondition Failed({"error":{"code":"PreconditionFailed"}})')
        # A PUT replaces the group's tags.
        groups[name]["tags"] = tags
        save()
        status(200)
    else:
        groups[name] = {"location": body["location"], "tags": tags, "vms": {}}
        save()
        status(201)
    if os.environ.get("FAKE_AZ_REST_FAIL_AFTER_APPLY"):
        # The server applied the PUT; the client still reports a failure.
        fail("ERROR: The operation timed out while reading the response.")
elif cmd == ("tag", "update"):
    m = re.fullmatch(r"/subscriptions/([^/]+)/resourceGroups/([^/]+)", opt("--resource-id") or "")
    if not m or m.group(1) != sub or opt("--operation") != "Merge":
        fail("fake az: unexpected tag update", 2)
    g = groups.get(m.group(2))
    if g is None:
        not_found("ResourceGroupNotFound", "Resource group could not be found.")
    replace = os.environ.get("FAKE_AZ_REPLACE_AFTER_CREATE")
    if replace:
        # Between the 201 and this call the group was deleted and someone
        # else created one of the same name (group IDs are name-based, so
        # this Merge reaches theirs). It then fails, applies, or applies and
        # reports an error.
        g = groups[m.group(2)] = {"location": g["location"], "tags": {"owner": "someone-else"},
                                  "vms": {"theirs": {"power": "running", "args": []}}}
        save()
        if replace not in ("merge", "merge-error"):
            fail("ERROR: (Conflict) the resource was modified")
    if os.environ.get("FAKE_AZ_TAG_UPDATE_FAIL") == "before":
        fail("ERROR: (AuthorizationFailed) not allowed to write tags")
    g["tags"].update(dict(t.split("=", 1) for t in opts("--tags")))
    save()
    if os.environ.get("FAKE_AZ_TAG_UPDATE_FAIL") == "after" or replace == "merge-error":
        fail("ERROR: The operation timed out while reading the response.")
elif cmd == ("group", "delete"):
    name = opt("--name")
    if "--yes" not in args:
        fail("fake az: group delete needs --yes", 2)
    if name not in groups:
        not_found("ResourceGroupNotFound", "Resource group '%s' could not be found." % name)
    del groups[name]
    save()
elif cmd == ("group", "list"):
    m = re.fullmatch(r'\[\]\.\[name((, tags\."[a-z-]+")+)\]', query or "")
    if not m:
        fail("fake az: unexpected list query " + str(query), 2)
    keys = re.findall(r'tags\."([a-z-]+)"', m.group(1))
    for name, g in sorted(groups.items()):
        if opt("--tag") and opt("--tag") not in g["tags"]:
            continue
        print("\t".join([name] + [g["tags"].get(k, "") for k in keys]))
elif cmd == ("vm", "create"):
    g = groups.get(opt("--resource-group"))
    if g is None:
        not_found("ResourceGroupNotFound", "Resource group could not be found.")
    if g["location"] != opt("--location"):
        fail("fake az: VM location differs from its group", 2)
    if fail_on == "vm-create-nothing":
        fail("ERROR: (QuotaExceeded) quota exceeded")
    if "--admin-password" in args:
        with open(log + ".secret", "a") as out:
            out.write(from_file(opt("--admin-password"), "--admin-password") + "\n")
    g["vms"][opt("--name")] = {"power": "running", "args": args}
    # Someone retags the build group once the build is under way.
    for env_key, tag in (("FAKE_AZ_RETAG_TAG", "image-build"), ("FAKE_AZ_RETAG_EXPIRES", "image-build-expires"),
                         ("FAKE_AZ_RETAG_OWNER", "image-build-owner"),
                         ("FAKE_AZ_RETAG_CREATING", "image-build-creating")):
        if os.environ.get(env_key):
            g["tags"][tag] = os.environ[env_key]
    save()
    if fail_on == "vm-create":
        fail("ERROR: (OSProvisioningTimedOut) OS Provisioning did not finish in the allotted time.")
elif cmd == ("vm", "run-command", "invoke"):
    vm = groups.get(opt("--resource-group"), {}).get("vms", {}).get(opt("--name"))
    if vm is None:
        not_found("ResourceNotFound", "VM not found")
    if query != "value[].message":
        fail("fake az: unexpected run-command query " + str(query), 2)
    script = "".join(from_file(s, "--scripts") for s in opts("--scripts"))
    with open(log + ".scripts", "a") as out:
        out.write("--- " + opt("--command-id") + "\n" + script)
    ok = "image-build-ssh-ready" in script and not os.environ.get("FAKE_AZ_RUNCMD_FAIL")
    print("Enable succeeded: \n[stdout]\n%s\n[stderr]\n%s" % (
        "image-build-ssh-ready" if ok else "", "" if ok else "Add-WindowsCapability : access denied"))
elif cmd == ("vm", "show"):
    vm = groups.get(opt("--resource-group"), {}).get("vms", {}).get(opt("--name"))
    if vm is None:
        not_found("ResourceNotFound", "VM not found")
    if query != "publicIps" or "--show-details" not in args:
        fail("fake az: unexpected vm show " + str(query), 2)
    print(os.environ.get("FAKE_AZ_IP", "192.0.2.30"))
elif cmd in (("vm", "deallocate"), ("vm", "restart")):
    vm = groups.get(opt("--resource-group"), {}).get("vms", {}).get(opt("--name"))
    if vm is None:
        not_found("ResourceNotFound", "VM not found")
    vm["power"] = "deallocated" if cmd[1] == "deallocate" else "running"
    save()
    if cmd[1] == "restart":
        # The fake build VM (FAKE_SSH) answers with a new boot ID from now on.
        with open(log + ".reboot-pending", "w") as out:
            out.write(os.environ.get("FAKE_BOOT_FLIP_AFTER", "0") + "\n")
else:
    fail("fake az: unrecognised command: " + " ".join(args), 2)
'''


def _azure_env(tmp_path: Path, os_family: str = "linux", config: dict = None, **overrides):
    recipe = {"BASE_AZURE": URN_WINDOWS if os_family == "windows" else URN_LINUX,
              "AZURE_VM_SIZE": "Standard_B2s"}
    recipe.update(overrides)
    env, recipe_path = _build_env(tmp_path, os_family, **{k: v for k, v in recipe.items() if v is not None})
    # The real backend, from the default directory next to build_image.sh.
    env.pop("IMAGE_BUILD_BACKENDS_DIR")
    cfg = {"AZ_SUBSCRIPTION_ID": SUB, "AZ_BUILD_LOCATION": LOCATION, "AZ_BUILD_SUBNET_ID": SUBNET_ID,
           "AZ_GALLERY_RG": GALLERY_RG, "AZ_GALLERY": GALLERY}
    cfg.update(config or {})
    (tmp_path / "config.env").write_text(
        "".join(f"{k}='{v}'\n" for k, v in cfg.items() if v is not None))
    az = tmp_path / "bin" / "az"
    az.write_text(FAKE_AZ)
    az.chmod(0o755)
    env["AZ_STATE"] = str(tmp_path / "az-state.json")
    return env, recipe_path


def _az(tmp_path: Path) -> list:
    """Every az call, as its argument list."""
    return [shlex.split(line)[1:] for line in _calls(tmp_path).splitlines() if line.startswith("az ")]


def _verb(argv: list) -> str:
    words = []
    for a in argv[:3]:
        if a.startswith("--"):
            break
        words.append(a)
    return " ".join(words)


def _opt(argv: list, name: str):
    return argv[argv.index(name) + 1] if name in argv else None


def _opts(argv: list, name: str) -> list:
    out = []
    for a in argv[argv.index(name) + 1:]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


def _one(tmp_path: Path, verb: str) -> list:
    calls = [a for a in _az(tmp_path) if _verb(a) == verb]
    assert len(calls) == 1, (verb, [_verb(a) for a in _az(tmp_path)])
    return calls[0]


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "az-state.json").read_text())


def _seed(tmp_path: Path, groups: dict = None, definitions: dict = None, versions: dict = None) -> None:
    (tmp_path / "az-state.json").write_text(json.dumps(
        {"groups": groups or {}, "definitions": definitions or {}, "versions": versions or {}}))


def _group(**tags) -> dict:
    """A seeded resource group. A build group (one with image-build) gets an
    ownership nonce and the matching creation token unless the test passes
    image_build_owner or image_build_creating itself."""
    tags = {k.replace("_", "-"): v for k, v in tags.items()}
    if "image-build" in tags:
        tags.setdefault("image-build-owner", "0123456789abcdef0123456789abcdef")
        tags.setdefault("image-build-creating", tags["image-build-owner"])
    tags = {k: v for k, v in tags.items() if v is not None}
    return {"location": LOCATION, "vms": {}, "tags": tags}


def _create(tmp_path: Path) -> tuple:
    """The one create-only PUT of the build's group: (run ID, body, argv)."""
    argv = _one(tmp_path, "rest")
    m = re.fullmatch(rf"/subscriptions/{SUB}/resourcegroups/([^/?]+)\?api-version=2021-04-01", _opt(argv, "--url"))
    assert m, argv
    return m.group(1), json.loads(_opt(argv, "--body")), argv


def _run_id(tmp_path: Path) -> str:
    return _create(tmp_path)[0]


def _only_token(tmp_path: Path) -> bool:
    """The group carries only this run's creation token: what the PUT
    wrote, which no reap can select."""
    _run, body, _argv = _create(tmp_path)
    tags = _state(tmp_path)["groups"][_run]["tags"]
    return tags == body["tags"] and set(tags) == {"image-build-creating"} \
        and NONCE_RE.match(tags["image-build-creating"]) is not None


def _gallery_key(definition: str, sub: str = SUB, rg: str = GALLERY_RG, gallery: str = GALLERY) -> str:
    return f"{sub}/{rg}/{gallery}/{definition}"


def _version_of(image_name: str) -> str:
    stamp = image_name.rsplit("-", 1)[1]
    return f"{int(stamp[:4])}.{int(stamp[4:8])}.{int(stamp[8:])}"


def _version_id(definition: str, version: str) -> str:
    return (f"/subscriptions/{SUB}/resourceGroups/{GALLERY_RG}/providers/Microsoft.Compute/galleries/"
            f"{GALLERY}/images/{definition}/versions/{version}")


def _image_name(tmp_path: Path) -> str:
    return _build_log(tmp_path).read_text().splitlines()[1].split("\t")[3]


def _text(tmp_path: Path, suffix: str) -> str:
    path = tmp_path / f"calls.log.{suffix}"
    return path.read_text() if path.exists() else ""


def test_azure_create_own_group_tags_and_subnet(tmp_path):
    env, recipe = _azure_env(tmp_path)
    result = _build(env, recipe, "azure")
    assert result.returncode == 0, result.stderr
    calls = _az(tmp_path)
    # Every call names the subscription; the CLI's default never decides. The
    # create-only PUT names it in its URL.
    for argv in calls:
        if _verb(argv) != "rest":
            assert _opt(argv, "--subscription") == SUB, argv

    # The build's own resource group, named after the run, tagged, in the
    # build location, created create-only (never `az group create`, an upsert).
    run_id, body, put = _create(tmp_path)
    assert RUN_ID_RE.match(run_id), run_id
    assert _opt(put, "--method") == "put" and _opt(put, "--headers") == "If-None-Match=*"
    assert "--debug" in put
    assert body["location"] == LOCATION
    # The PUT itself carries nothing a reap could match: only the creation
    # token, this run's nonce.
    assert set(body["tags"]) == {"image-build-creating"}
    assert NONCE_RE.match(body["tags"]["image-build-creating"])
    # Only after the 201: the run's tags, merged on, then read back exactly.
    update = _one(tmp_path, "tag update")
    assert _opt(update, "--resource-id") == f"/subscriptions/{SUB}/resourceGroups/{run_id}"
    assert _opt(update, "--operation") == "Merge"
    group_tags = dict(t.split("=", 1) for t in _opts(update, "--tags"))
    assert set(group_tags) == {"image-build", "image-build-expires", "image-build-owner"}
    assert group_tags["image-build"] == run_id
    assert re.fullmatch(r"[0-9]{12}", group_tags["image-build-expires"])
    # The Merge's owner tag is the same nonce, and it keeps the token.
    assert group_tags["image-build-owner"] == body["tags"]["image-build-creating"]
    tags = {k: group_tags[k] for k in ("image-build", "image-build-expires")}
    readback = next(a for a in calls[calls.index(update):] if _verb(a) == "group show")
    assert _opt(readback, "--name") == run_id
    assert "group create" not in [_verb(a) for a in calls]
    assert calls.index(put) < calls.index(update) < calls.index(readback) < calls.index(_one(tmp_path, "vm create"))
    # The debug log (it holds a token prefix) never reaches the output.
    assert "Bearer" not in result.stderr and "Response status" not in result.stderr

    # The VM: in that group, in the build subnet, no NIC NSG, a public IP of its own.
    vm = _one(tmp_path, "vm create")
    for name, value in (("--resource-group", run_id), ("--name", run_id), ("--location", LOCATION),
                        ("--image", f"Canonical:ubuntu-24_04-lts:server:{LATEST}"),
                        ("--size", "Standard_B2s"), ("--os-disk-size-gb", "20"),
                        ("--subnet", SUBNET_ID), ("--nsg", ""), ("--public-ip-address", f"{run_id}-ip"),
                        ("--public-ip-sku", "Standard"), ("--admin-username", "builder"),
                        ("--authentication-type", "ssh"), ("--output", "none")):
        assert _opt(vm, name) == value, (name, vm)
    assert dict(t.split("=", 1) for t in _opts(vm, "--tags")) == tags
    assert "--admin-password" not in vm and "--specialized" not in vm
    # The key goes in from the run's key file.
    assert Path(_opt(vm, "--ssh-key-values")).name == "key.pub"
    assert "run-command" not in " ".join(_verb(a) for a in calls)
    assert any(l.startswith("ssh ") and f" builder@{PUBLIC_IP} " in l for l in _calls(tmp_path).splitlines())

    # Stdout carries only the templates.yaml line.
    image_name = _image_name(tmp_path)
    image_id = _version_id("example-lab-linux", _version_of(image_name))
    assert result.stdout == f"azure: {{image: {image_id}, vm_size: Standard_B2s, disk_gb: 20}}\n"
    assert _build_log(tmp_path).read_text().splitlines()[1].split("\t")[2] == image_id
    # The build group is gone; the version exists.
    state = _state(tmp_path)
    assert state["groups"] == {}
    assert list(state["versions"]) == [_gallery_key("example-lab-linux") + "/" + _version_of(image_name)]
    text = _calls(tmp_path) + result.stderr + result.stdout
    assert "lab-playground-site" not in text
    assert KEY_MATERIAL not in text


def test_azure_windows_run_command_installs_openssh(tmp_path):
    env, recipe = _azure_env(tmp_path, "windows")
    result = _build(env, recipe, "azure")
    assert result.returncode == 0, result.stderr
    vm = _one(tmp_path, "vm create")
    run_id = _opt(vm, "--name")
    assert _opt(vm, "--image") == f"MicrosoftWindowsServer:WindowsServer:2022-datacenter-g2:{LATEST}"
    assert _opt(vm, "--admin-username") == "builder"
    assert _opt(vm, "--authentication-type") == "password"
    assert "--ssh-key-values" not in vm
    computer = _opt(vm, "--computer-name")
    assert re.fullmatch(r"[a-z0-9-]{1,15}", computer), computer
    # The password reaches az only as @file.
    assert _opt(vm, "--admin-password").startswith("@")

    # The key goes in through run-command, after the VM exists.
    calls = _az(tmp_path)
    run = _one(tmp_path, "vm run-command invoke")
    assert calls.index(vm) < calls.index(run)
    assert _opt(run, "--command-id") == "RunPowerShellScript"
    assert _opt(run, "--resource-group") == run_id and _opt(run, "--name") == run_id
    assert _opt(run, "--scripts").startswith("@")
    scripts = _text(tmp_path, "scripts")
    assert scripts.startswith("--- RunPowerShellScript\n")
    for needle in ("OpenSSH.Server~~~~0.0.1.0", "Add-WindowsCapability",
                   "administrators_authorized_keys", "icacls", "/inheritance:r",
                   "*S-1-5-32-544:F", "*S-1-5-18:F", "Start-Service", "sshd",
                   "-LocalPort 22", "Enable-LocalUser", "RandomNumberGenerator", f"'{PUBKEY}'"):
        assert needle in scripts, needle
    assert scripts.rstrip().endswith(f"Write-Output '{READY}'")
    assert any(l.startswith("ssh ") and f" Administrator@{PUBLIC_IP} " in l
               for l in _calls(tmp_path).splitlines())

    # The admin password is random, meets Azure's rules, and is never echoed,
    # logged, put in the script or left on disk.
    secrets = _text(tmp_path, "secret").splitlines()
    assert len(secrets) == 1
    password = secrets[0]
    assert 12 <= len(password) <= 123
    assert sum(bool(re.search(p, password)) for p in ("[a-z]", "[A-Z]", "[0-9]", "[^A-Za-z0-9]")) >= 3
    for text in (_calls(tmp_path), scripts, result.stdout, result.stderr, _text(tmp_path, "remote")):
        assert password not in text
    assert list((tmp_path / "tmp").iterdir()) == []
    assert KEY_MATERIAL not in _calls(tmp_path) + scripts + result.stdout + result.stderr

    # A second build gets another password.
    env, recipe = _azure_env(tmp_path / "again", "windows")
    assert _build(env, recipe, "azure").returncode == 0
    assert _text(tmp_path / "again", "secret").splitlines() != secrets

    # run-command reports success even when the script failed: without the
    # script's last line, the build fails and the group is deleted.
    env, recipe = _azure_env(tmp_path / "runfail", "windows")
    env["FAKE_AZ_RUNCMD_FAIL"] = "1"
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    assert "did not finish" in result.stderr and "access denied" in result.stderr
    assert not any(l.startswith("ssh ") for l in _calls(tmp_path / "runfail").splitlines())
    assert _verb(_az(tmp_path / "runfail")[-1]) == "group delete"
    assert _state(tmp_path / "runfail")["groups"] == {}


def test_azure_specialized_gallery_base(tmp_path):
    # A base this backend built (specialized): no admin profile; the key goes
    # in through run-command, for builder.
    base_def = "docker-host"
    base_id = (f"/subscriptions/{OTHER_SUB}/resourceGroups/base-rg/providers/Microsoft.Compute/galleries/"
               f"base_gallery/images/{base_def}/versions/2026.1101.1200")
    env, recipe = _azure_env(tmp_path, BASE_AZURE=base_id)
    _seed(tmp_path, definitions={
        f"{OTHER_SUB}/base-rg/base_gallery/{base_def}": {"osType": "Linux", "osState": "Specialized",
                                                          "hyperVGeneration": "V1"}},
        versions={f"{OTHER_SUB}/base-rg/base_gallery/{base_def}/2026.1101.1200": {"state": "Succeeded"}})
    result = _build(env, recipe, "azure")
    assert result.returncode == 0, result.stderr
    # The base is read in its own subscription.
    show = [a for a in _az(tmp_path) if _verb(a) == "sig image-version show"
            and _opt(a, "--gallery-name") == "base_gallery"]
    assert len(show) == 1 and _opt(show[0], "--subscription") == OTHER_SUB
    vm = _one(tmp_path, "vm create")
    assert _opt(vm, "--image") == base_id
    assert "--specialized" in vm
    for absent in ("--admin-username", "--admin-password", "--ssh-key-values", "--computer-name"):
        assert absent not in vm, absent
    run = _one(tmp_path, "vm run-command invoke")
    assert _opt(run, "--command-id") == "RunShellScript"
    scripts = _text(tmp_path, "scripts")
    assert f"key='{PUBKEY}'" in scripts and "/home/builder/.ssh/authorized_keys" in scripts
    assert scripts.rstrip().endswith(f"echo '{READY}'")
    assert any(f" builder@{PUBLIC_IP} " in l for l in _calls(tmp_path).splitlines() if l.startswith("ssh "))
    # The new definition takes the base's generation.
    definition = _state(tmp_path)["definitions"][_gallery_key("example-lab-linux")]
    assert definition["hyperVGeneration"] == "V1"


def test_azure_resolve_base(tmp_path):
    # A URN with a fixed version is looked up as written.
    env, recipe = _azure_env(tmp_path / "fixed", BASE_AZURE="Canonical:ubuntu-24_04-lts:server:24.04.202610150")
    assert _build(env, "--dry-run", recipe, "azure").returncode == 0
    env["FAKE_AZ_LATEST"] = "ignored"
    result = _build(env, recipe, "azure")
    assert result.returncode == 0, result.stderr
    assert _opt(_one(tmp_path / "fixed", "vm create"), "--image") == "Canonical:ubuntu-24_04-lts:server:24.04.202610150"

    # A URN that does not exist fails with the pattern, before anything is made.
    env, recipe = _azure_env(tmp_path / "missing")
    env["FAKE_AZ_MISSING_URNS"] = URN_LINUX
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    assert URN_LINUX in result.stderr
    verbs = [_verb(a) for a in _az(tmp_path / "missing")]
    assert verbs == ["vm image show"], verbs

    # A gallery version that does not exist, or did not provision, fails too.
    version = (f"/subscriptions/{SUB}/resourceGroups/base-rg/providers/Microsoft.Compute/galleries/"
               "base_gallery/images/base/versions/1.0.0")
    for sub, versions in (("nover", {}),
                          ("failed", {f"{SUB}/base-rg/base_gallery/base/1.0.0": {"state": "Failed"}})):
        env, recipe = _azure_env(tmp_path / sub, BASE_AZURE=version)
        _seed(tmp_path / sub, versions=versions)
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, sub
        assert version in result.stderr, (sub, result.stderr)
        assert [_verb(a) for a in _az(tmp_path / sub)] == ["sig image-version show"], sub

    # The base must be one of the two forms.
    for i, bad in enumerate(("Canonical:ubuntu-24_04-lts:server", "ubuntu", "a:b:c:d:e",
                             "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Compute/images/img",
                             version.rsplit("/", 2)[0])):
        env, recipe = _azure_env(tmp_path / f"bad{i}", BASE_AZURE=bad)
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, bad
        assert "marketplace URN publisher:offer:sku:version or a gallery image-version ID" in result.stderr, bad
        assert _az(tmp_path / f"bad{i}") == [], bad

    # A base of the other OS family is refused before a group exists.
    env, recipe = _azure_env(tmp_path / "os", BASE_AZURE=URN_WINDOWS)
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    assert "OS_FAMILY" in result.stderr
    assert "rest" not in [_verb(a) for a in _az(tmp_path / "os")]


def _rid(stamp: str, c: str) -> str:
    """A run ID in the core's format: ib-<stamp>-<16 hex>."""
    return f"ib-{stamp}-{c * 16}"


def test_azure_leftovers_only_tagged_expired(tmp_path):
    old_1, old_2, young = _rid("202601010000", "a"), _rid("202601010000", "b"), _rid("209901010000", "c")
    leap = _rid("202402290000", "9")
    bad_expiry = {_rid("202601010000", c): e for c, e in (
        ("1", "soon"), ("2", None), ("3", "000000000000"), ("4", "202613010000"),
        ("5", "202602300000"), ("6", "202601011260"), ("7", "202302290000"), ("8", "not-owned"))}
    groups = {
        old_1: _group(image_build=old_1, image_build_expires=EXPIRED),
        old_2: _group(image_build=old_2, image_build_expires=EXPIRED),
        leap: _group(image_build=leap, image_build_expires="202402290000"),
        young: _group(image_build=young, image_build_expires=UNEXPIRED),
        "lab-rg": _group(lab_playground_site="site-1"),
        "untagged-rg": _group(),
        "renamed-rg": _group(image_build=old_1, image_build_expires=EXPIRED),
        _rid("202601010000", "d"): _group(image_build="not-a-run", image_build_expires=EXPIRED),
        _rid("202601010000", "e"): _group(image_build=_rid("202601010000", "e"), image_build_expires=EXPIRED,
                                         image_build_owner=None),
        _rid("202601010000", "f"): _group(image_build=_rid("202601010000", "f"), image_build_expires=EXPIRED,
                                         image_build_owner="not-owned"),
        "ib-202601010000-0000": _group(image_build="ib-202601010000-0000", image_build_expires=EXPIRED),
        # Run tags merged onto a group this run's PUT never made: no creation
        # token, or one that is not the owner nonce.
        _rid("202601010000", "c"): _group(image_build=_rid("202601010000", "c"), image_build_expires=EXPIRED,
                                         image_build_creating=None),
        _rid("202601010000", "0"): _group(image_build=_rid("202601010000", "0"), image_build_expires=EXPIRED,
                                         image_build_creating="f" * 32),
        **{name: _group(image_build=name, image_build_expires=e) for name, e in bad_expiry.items()},
    }
    env, _recipe = _azure_env(tmp_path)
    _seed(tmp_path, groups)
    reaped = sorted([old_1, old_2, leap])

    result = _build(env, "--reap", "azure", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "".join(f"would destroy {n}\n" for n in reaped)
    assert "group delete" not in [_verb(a) for a in _az(tmp_path)]
    assert f"DRY-RUN: az group delete --subscription {SUB} --name {old_1} --yes" in result.stderr
    assert _state(tmp_path)["groups"].keys() == groups.keys()

    result = _build(env, "--reap", "azure")
    assert result.returncode == 0, result.stderr
    listing = [a for a in _az(tmp_path) if _verb(a) == "group list"]
    assert listing and all(_opt(a, "--tag") == "image-build" for a in listing)
    deletes = [_opt(a, "--name") for a in _az(tmp_path) if _verb(a) == "group delete"]
    assert deletes == reaped
    assert set(_state(tmp_path)["groups"]) == set(groups) - set(reaped)
    # A group with the tag that is not a reapable build group is named, not
    # deleted: no valid expiry (impossible dates included), no nonce, no
    # matching creation token.
    for name in [*bad_expiry, *(_rid("202601010000", c) for c in "efc0")]:
        assert f"resource group {name}:" in result.stderr, name
    assert young not in result.stderr

    result = _build(env, "--reap", "azure")
    assert result.returncode == 0, result.stderr
    assert "nothing to reap" in result.stderr


def test_azure_destroy_rechecks_tags(tmp_path):
    # Reap: the listing said build group, the re-read tag (or expiry) says
    # otherwise.
    old = _rid("202601010000", "a")
    for sub, override, shown in (("reap", {"FAKE_AZ_SHOW_TAG": "someone-else"}, "someone-else"),
                                 ("reap-zero", {"FAKE_AZ_SHOW_EXPIRES": "000000000000"}, "000000000000"),
                                 ("reap-feb30", {"FAKE_AZ_SHOW_EXPIRES": "202602300000"}, "202602300000")):
        env, _recipe = _azure_env(tmp_path / sub)
        _seed(tmp_path / sub, {old: _group(image_build=old, image_build_expires=EXPIRED)})
        env.update(override)
        result = _build(env, "--reap", "azure")
        assert result.returncode == 1, sub
        shows = [a for a in _az(tmp_path / sub) if _verb(a) == "group show"]
        assert [_opt(a, "--name") for a in shows] == [old], sub
        assert "group delete" not in [_verb(a) for a in _az(tmp_path / sub)], sub
        assert shown in result.stderr, (sub, result.stderr)
        assert old in _state(tmp_path / sub)["groups"], sub

    # Reap: a group gone between the listing and the delete is not an error.
    env, _recipe = _azure_env(tmp_path / "gone")
    _seed(tmp_path / "gone", {old: _group(image_build=old, image_build_expires=EXPIRED)})
    script = (f"source {shlex.quote(str(AZURE_BACKEND))}\n"
              f"AZ_SUBSCRIPTION_ID={SUB}\nDRY_RUN=0\n"
              "query() { shift; \"$@\"; }\nmutate() { \"$@\"; }\ndie() { echo \"$*\" >&2; exit 1; }\n"
              f"az group delete --subscription {SUB} --name {old} --yes\n"
              f"backend_destroy_id {old}\n")
    gone = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=env, capture_output=True,
                          text=True, timeout=30)
    assert gone.returncode == 0, gone.stderr
    assert "no longer exists" in gone.stderr

    # Build: a group whose tag, expiry or ownership nonce is not this run's
    # is never deleted.
    for sub, override in (("build", {"FAKE_AZ_RETAG_TAG": _rid("202601010000", "f")}),
                          ("build-expiry", {"FAKE_AZ_RETAG_EXPIRES": "202601010000"}),
                          ("build-owner", {"FAKE_AZ_RETAG_OWNER": "f" * 32}),
                          ("build-creating", {"FAKE_AZ_RETAG_CREATING": "f" * 32})):
        env, recipe = _azure_env(tmp_path / sub)
        env.update(override)
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, sub
        run_id = _run_id(tmp_path / sub)
        assert _verb(_az(tmp_path / sub)[-1]) == "group show", sub
        assert "group delete" not in [_verb(a) for a in _az(tmp_path / sub)], sub
        assert "--reap azure" in result.stderr, sub
        assert run_id in _state(tmp_path / sub)["groups"], sub

    # Build: a failed verify deletes this run's group, after re-reading it.
    env, recipe = _azure_env(tmp_path / "verify")
    env.update(FAKE_SSH_FAIL_ON="verify.sh", FAKE_SSH_FAIL_RC="3")
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    calls = _az(tmp_path / "verify")
    run_id = _run_id(tmp_path / "verify")
    assert _verb(calls[-2]) == "group show" and _opt(calls[-2], "--name") == run_id
    assert calls[-1] == ["group", "delete", "--subscription", SUB, "--name", run_id, "--yes", "--output", "none"]
    assert "sig image-version create" not in [_verb(a) for a in calls]
    assert _state(tmp_path / "verify")["groups"] == {}

    # Build: vm create failed after the VM came to exist; the group goes.
    env, recipe = _azure_env(tmp_path / "create")
    env["FAKE_AZ_FAIL"] = "vm-create"
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    assert _verb(_az(tmp_path / "create")[-1]) == "group delete"
    assert _state(tmp_path / "create")["groups"] == {}
    assert "could not destroy" not in result.stderr

    # Build: the group PUT failed (403): this run owns nothing, so there is
    # nothing to re-read or delete.
    env, recipe = _azure_env(tmp_path / "nothing")
    env["FAKE_AZ_FAIL"] = "group-create"
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    verbs = [_verb(a) for a in _az(tmp_path / "nothing")]
    assert verbs[-1] == "rest" and "group delete" not in verbs and "group show" not in verbs
    assert "AuthorizationFailed" in result.stderr and "HTTP 403" in result.stderr
    assert "could not destroy" not in result.stderr
    assert "Bearer" not in result.stderr


def _reap_later(tmp_path: Path, env: dict):
    """`--reap azure` run as if it were 2099: anything with this run's tags
    would be long expired by then."""
    later = tmp_path / "later-bin"
    later.mkdir(exist_ok=True)
    (later / "date").write_text(
        "#!/bin/sh\n"
        'if [ "$*" = "-u +%Y%m%d%H%M" ]; then echo 209912312359; exit 0; fi\n'
        f'exec {shlex.quote(shutil.which("date"))} "$@"\n')
    (later / "date").chmod(0o755)
    return _build({**env, "PATH": f"{later}:{env['PATH']}"}, "--reap", "azure")


def test_azure_group_create_is_create_only(tmp_path):
    """`az group create` is an upsert: a same-name group made by anyone before
    the create would be adopted, retagged and later deleted. The group is PUT
    with If-None-Match: * and only the tag image-build-creating, which no reap
    matches; only a confirmed 201 Created makes it this run's. Whatever an
    ambiguous or failed create leaves, a later reap can neither list nor
    delete it."""
    def foreign_intact(sub):
        return _state(tmp_path / sub)["groups"][_run_id(tmp_path / sub)]["vms"] == {
            "theirs": {"power": "running", "args": []}}

    # Someone creates the group just before the PUT; Azure honours the
    # condition (412): nothing changed, nothing owned, nothing deleted.
    env, recipe = _azure_env(tmp_path / "race")
    env["FAKE_AZ_RACE"] = "1"
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    assert "already exists; not used, not changed" in result.stderr
    verbs = [_verb(a) for a in _az(tmp_path / "race")]
    assert verbs[-1] == "rest", verbs
    assert not {"vm create", "group show", "group delete", "tag update"} & set(verbs)
    group = _state(tmp_path / "race")["groups"][_run_id(tmp_path / "race")]
    assert group["tags"] == {"owner": "someone-else"}
    assert foreign_intact("race")

    # The same race where Azure ignores the condition and replaces the
    # group's tags (200): with az reporting success, with az failing after
    # the server applied it, and with a tag write that would fail. None of
    # them owns the group, writes to it again or deletes it, and a reap run
    # long after the build's expiry neither lists nor deletes it.
    for sub, extra, said in (("upsert", {}, "HTTP 200, not a confirmed 201"),
                             ("upsert-az-fails", {"FAKE_AZ_REST_FAIL_AFTER_APPLY": "1"}, "az exit 1, HTTP 200"),
                             ("upsert-tag-fails", {"FAKE_AZ_TAG_UPDATE_FAIL": "before"},
                              "HTTP 200, not a confirmed 201")):
        env, recipe = _azure_env(tmp_path / sub)
        env.update(FAKE_AZ_RACE="1", FAKE_AZ_IGNORE_IF_NONE_MATCH="1", **extra)
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, sub
        assert said in result.stderr, (sub, result.stderr)
        run_id = _run_id(tmp_path / sub)
        assert run_id in result.stderr, sub
        verbs = [_verb(a) for a in _az(tmp_path / sub)]
        assert verbs[-1] == "rest", (sub, verbs)
        assert foreign_intact(sub), sub
        assert _only_token(tmp_path / sub), sub
        later = _reap_later(tmp_path / sub, env)
        assert later.returncode == 0, (sub, later.stderr)
        assert later.stdout == "" and "nothing to reap" in later.stderr, (sub, later.stdout, later.stderr)
        assert "group delete" not in [_verb(a) for a in _az(tmp_path / sub)], sub
        assert foreign_intact(sub), sub

    # A fresh create whose status cannot be read, or that az reports as
    # failed after the server made it: not ownership, and not reapable.
    for sub, extra in (("nostatus", {"FAKE_AZ_REST_NO_STATUS": "1"}),
                       ("created-az-fails", {"FAKE_AZ_REST_FAIL_AFTER_APPLY": "1"})):
        env, recipe = _azure_env(tmp_path / sub)
        env.update(extra)
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, sub
        assert "not a confirmed 201 Created" in result.stderr, (sub, result.stderr)
        run_id = _run_id(tmp_path / sub)
        verbs = [_verb(a) for a in _az(tmp_path / sub)]
        assert verbs[-1] == "rest", (sub, verbs)
        assert _only_token(tmp_path / sub), sub
        later = _reap_later(tmp_path / sub, env)
        assert later.returncode == 0 and later.stdout == "", (sub, later.stdout, later.stderr)
        assert run_id in _state(tmp_path / sub)["groups"], sub

    # The PUT answered 200 (a takeover), but another request's 201 is logged
    # after it; or a fresh create whose status line appears twice. Neither is
    # a 201 for this request: nothing owned, tagged or deleted, nothing reapable.
    for sub, extra in (("stray-201", {"FAKE_AZ_RACE": "1", "FAKE_AZ_IGNORE_IF_NONE_MATCH": "1",
                                      "FAKE_AZ_STRAY_201": "1"}),
                       ("double-status", {"FAKE_AZ_DOUBLE_STATUS": "1"})):
        env, recipe = _azure_env(tmp_path / sub)
        env.update(extra)
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, (sub, result.stderr)
        assert "not a confirmed 201 Created" in result.stderr, (sub, result.stderr)
        run_id = _run_id(tmp_path / sub)
        verbs = [_verb(a) for a in _az(tmp_path / sub)]
        assert verbs[-1] == "rest", (sub, verbs)
        assert _only_token(tmp_path / sub), sub
        later = _reap_later(tmp_path / sub, env)
        assert later.returncode == 0 and later.stdout == "", (sub, later.stdout, later.stderr)
        assert "group delete" not in [_verb(a) for a in _az(tmp_path / sub)], sub
    assert foreign_intact("stray-201")

    for sub in ("race", "upsert", "upsert-az-fails", "nostatus", "stray-201"):
        assert "Bearer" not in _calls(tmp_path / sub)


def test_azure_post_create_tagging_failure(tmp_path):
    """After a 201 the run tags the group as its own and reads the tags back.
    If either fails, it deletes nothing: a parsed 201 is history, and the
    group may since have been replaced. The build stops and names the group
    with the command to delete it by hand. A same-name replacement that even
    received this run's Merge lacks the creation token, so it is never owned
    and never reapable."""
    envs = {}
    for sub, extra in (("tag-fails", {"FAKE_AZ_TAG_UPDATE_FAIL": "before"}),
                       ("tag-applied-az-fails", {"FAKE_AZ_TAG_UPDATE_FAIL": "after"}),
                       ("readback-empty", {"FAKE_AZ_EMPTY": "group show"}),
                       ("readback-denied", {"FAKE_AZ_ERROR": "group show|AuthorizationFailed|1"}),
                       ("replaced", {"FAKE_AZ_REPLACE_AFTER_CREATE": "fail"}),
                       ("replaced-merged", {"FAKE_AZ_REPLACE_AFTER_CREATE": "merge"}),
                       ("replaced-merged-az-fails", {"FAKE_AZ_REPLACE_AFTER_CREATE": "merge-error"})):
        env, recipe = _azure_env(tmp_path / sub)
        env.update(extra)
        envs[sub] = env
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, sub
        run_id = _run_id(tmp_path / sub)
        assert "deletes nothing" in result.stderr, (sub, result.stderr)
        assert f"delete it by hand: az group delete --subscription {SUB} --name {run_id}" in result.stderr, sub
        verbs = [_verb(a) for a in _az(tmp_path / sub)]
        assert "group delete" not in verbs and "vm create" not in verbs, (sub, verbs)
        assert run_id in _state(tmp_path / sub)["groups"], sub

    # The group was replaced by someone else's: whether this run's Merge
    # failed, reached it, or reached it and reported an error, it survives
    # untouched (bar the merged tags), and a reap long after the build's
    # expiry neither lists nor deletes it.
    for sub in ("replaced", "replaced-merged", "replaced-merged-az-fails"):
        run_id = _run_id(tmp_path / sub)
        group = _state(tmp_path / sub)["groups"][run_id]
        assert group["vms"] == {"theirs": {"power": "running", "args": []}}, sub
        assert group["tags"]["owner"] == "someone-else" and "image-build-creating" not in group["tags"], sub
        if sub != "replaced":
            # The Merge landed on the replacement: run ID, expiry and owner
            # nonce, everything but the creation token.
            assert group["tags"]["image-build"] == run_id, sub
        later = _reap_later(tmp_path / sub, envs[sub])
        assert later.returncode == 0 and later.stdout == "", (sub, later.stdout, later.stderr)
        assert "group delete" not in [_verb(a) for a in _az(tmp_path / sub)], sub
        assert _state(tmp_path / sub)["groups"][run_id] == group, sub

    # A failed tag write leaves only the non-reapable creation token.
    assert _only_token(tmp_path / "tag-fails")


def test_azure_reads_fail_closed(tmp_path):
    """Only an exact not-found code (with az's exit 3) reads as absent; an
    empty or malformed success never reads as found-and-fine."""
    # Cleanup after a failed verify: a group read that fails for any other
    # reason keeps the group and says to reap. (FAKE_AZ_FAULT_SKIP lets the
    # read-back after the create through; the fault hits the cleanup's read.)
    for sub, error in (("subscription", "group show|SubscriptionNotFound|3"),
                       ("auth", "group show|AuthorizationFailed|1"),
                       ("tenant", "group show|InvalidAuthenticationTokenTenant|1"),
                       ("not-exit-3", "group show|ResourceGroupNotFound|1")):
        env, recipe = _azure_env(tmp_path / sub)
        env.update(FAKE_SSH_FAIL_ON="verify.sh", FAKE_SSH_FAIL_RC="3", FAKE_AZ_ERROR=error, FAKE_AZ_FAULT_SKIP="1")
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, sub
        assert "not treated as absent" in result.stderr, (sub, result.stderr)
        assert "could not destroy" in result.stderr, sub
        assert "group delete" not in [_verb(a) for a in _az(tmp_path / sub)], sub
        assert _run_id(tmp_path / sub) in _state(tmp_path / sub)["groups"], sub

    # A group read that succeeds but prints nothing is not this run's group.
    env, recipe = _azure_env(tmp_path / "empty")
    env.update(FAKE_SSH_FAIL_ON="verify.sh", FAKE_SSH_FAIL_RC="3", FAKE_AZ_EMPTY="group show",
               FAKE_AZ_FAULT_SKIP="1")
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    assert "not deleted" in result.stderr and "could not destroy" in result.stderr
    assert "group delete" not in [_verb(a) for a in _az(tmp_path / "empty")]

    # Reap: a subscription error is not "no longer exists".
    old = _rid("202601010000", "a")
    env, _recipe = _azure_env(tmp_path / "reap")
    _seed(tmp_path / "reap", {old: _group(image_build=old, image_build_expires=EXPIRED)})
    env["FAKE_AZ_ERROR"] = "group show|SubscriptionNotFound|3"
    result = _build(env, "--reap", "azure")
    assert result.returncode == 1
    assert "no longer exists" not in result.stderr and "SubscriptionNotFound" in result.stderr
    assert old in _state(tmp_path / "reap")["groups"]

    # Save: the version and definition reads stop the save before the VM is
    # deallocated unless they say exactly "not found" or return valid facts.
    for sub, extra, message in (
            ("v-auth", {"FAKE_AZ_ERROR": "sig image-version show|AuthorizationFailed|1"}, "not treated as absent"),
            ("v-sub", {"FAKE_AZ_ERROR": "sig image-version show|SubscriptionNotFound|3"}, "not treated as absent"),
            ("v-empty", {"FAKE_AZ_EMPTY": "sig image-version show"}, "cannot tell whether it exists"),
            ("v-odd", {"FAKE_AZ_VERSION_ID_OUTPUT": "/subscriptions/x/versions/other"},
             "cannot tell whether it exists"),
            ("d-parent", {"FAKE_AZ_ERROR": "sig image-definition show|ParentResourceNotFound|3"},
             "not treated as absent"),
            ("d-empty", {"FAKE_AZ_EMPTY": "sig image-definition show"}, "not its OS type, state and generation")):
        env, recipe = _azure_env(tmp_path / sub)
        env.update(extra)
        result = _build(env, recipe, "azure")
        assert result.returncode == 1, sub
        assert message in result.stderr, (sub, result.stderr)
        verbs = [_verb(a) for a in _az(tmp_path / sub)]
        assert not {"vm deallocate", "sig image-definition create", "sig image-version create"} & set(verbs), (
            sub, verbs)
        assert verbs[-1] == "group delete", (sub, verbs)
        assert not _build_log(tmp_path / sub).exists(), sub


def test_azure_save_deallocates_before_publishing(tmp_path):
    env, recipe = _azure_env(tmp_path)
    result = _build(env, recipe, "azure")
    assert result.returncode == 0, result.stderr
    calls = _az(tmp_path)
    verbs = [_verb(a) for a in calls]
    run_id = _run_id(tmp_path)
    image_name = _image_name(tmp_path)
    version = _version_of(image_name)
    dealloc = verbs.index("vm deallocate")
    create = verbs.index("sig image-version create")
    assert dealloc < create < verbs.index("group delete")
    assert calls[dealloc] == ["vm", "deallocate", "--subscription", SUB, "--resource-group", run_id,
                              "--name", run_id, "--output", "none"]
    # The version: named from the build timestamp, from the VM, tagged.
    pub = calls[create]
    assert re.fullmatch(r"[1-9][0-9]{3}\.[1-9][0-9]{0,3}\.[0-9]{1,4}", version), version
    for name, value in (("--resource-group", GALLERY_RG), ("--gallery-name", GALLERY),
                        ("--gallery-image-definition", "example-lab-linux"),
                        ("--gallery-image-version", version), ("--location", LOCATION),
                        ("--virtual-machine", f"/subscriptions/{SUB}/resourceGroups/{run_id}/providers/"
                                              f"Microsoft.Compute/virtualMachines/{run_id}")):
        assert _opt(pub, name) == value, (name, pub)
    assert _opts(pub, "--tags") == ["image-build-recipe=labs/demo/recipe.env", "image-build-commit=abc1234"]
    # It was checked absent first, and the missing definition was created
    # specialized, with the base's generation.
    assert verbs.index("sig image-version show") < verbs.index("sig image-definition show") < dealloc
    definition = _state(tmp_path)["definitions"][_gallery_key("example-lab-linux")]
    assert definition == {"osType": "Linux", "osState": "Specialized", "hyperVGeneration": "V2",
                          "publisher": "image-build", "offer": "example-lab-linux", "sku": "example-lab-linux"}
    assert "generalize" not in " ".join(verbs) and "sysprep" not in _calls(tmp_path)
    assert _state(tmp_path)["versions"][_gallery_key("example-lab-linux") + "/" + version]["tags"] == {
        "image-build-recipe": "labs/demo/recipe.env", "image-build-commit": "abc1234"}
    assert "lab-playground-site" not in _calls(tmp_path)

    # An existing definition of the right kind is reused, not created.
    env, recipe = _azure_env(tmp_path / "reuse")
    _seed(tmp_path / "reuse", definitions={_gallery_key("example-lab-linux"): {
        "osType": "Linux", "osState": "Specialized", "hyperVGeneration": "V2"}})
    assert _build(env, recipe, "azure").returncode == 0
    assert "sig image-definition create" not in [_verb(a) for a in _az(tmp_path / "reuse")]

    # A definition of another kind (generalized) stops the save before the
    # VM is touched; nothing is logged.
    env, recipe = _azure_env(tmp_path / "kind")
    _seed(tmp_path / "kind", definitions={_gallery_key("example-lab-linux"): {
        "osType": "Linux", "osState": "Generalized", "hyperVGeneration": "V2"}})
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    assert "Linux/Generalized/V2" in result.stderr
    verbs = [_verb(a) for a in _az(tmp_path / "kind")]
    assert "vm deallocate" not in verbs and "sig image-version create" not in verbs
    assert not _build_log(tmp_path / "kind").exists()

    # A failed publish leaves its version, names it, and records nothing.
    env, recipe = _azure_env(tmp_path / "fail")
    env["FAKE_AZ_FAIL"] = "version-create"
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    versions = _state(tmp_path / "fail")["versions"]
    assert len(versions) == 1
    version = list(versions)[0].rsplit("/", 1)[1]
    assert _version_id("example-lab-linux", version) in result.stderr
    assert "az sig image-version delete" in result.stderr
    assert not _build_log(tmp_path / "fail").exists()
    assert result.stdout == ""
    assert _state(tmp_path / "fail")["groups"] == {}


def test_azure_never_overwrites_a_version(tmp_path):
    # A version of the same name (same prefix, same minute) already exists:
    # the read returns that version's own ID.
    env, recipe = _azure_env(tmp_path)
    env["FAKE_AZ_VERSION_EXISTS"] = "1"
    result = _build(env, recipe, "azure")
    assert result.returncode == 1
    assert "already exists; the builder never overwrites an image" in result.stderr
    verbs = [_verb(a) for a in _az(tmp_path)]
    assert "vm deallocate" not in verbs and "sig image-version create" not in verbs
    assert verbs[-1] == "group delete"


def test_azure_disk_gb_override(tmp_path):
    # AZURE_DISK_GB sizes the OS disk (Windows marketplace images are 127 GB);
    # the other backends' overrides are ignored here.
    env, recipe = _azure_env(tmp_path / "set", "windows", DISK_GB="40", AZURE_DISK_GB="127",
                             AWS_DISK_GB="64", GCP_DISK_GB="50")
    result = _build(env, recipe, "azure")
    assert result.returncode == 0, result.stderr
    assert _opt(_one(tmp_path / "set", "vm create"), "--os-disk-size-gb") == "127"
    assert result.stdout.endswith("vm_size: Standard_B2s, disk_gb: 127}\n"), result.stdout
    # Unset: DISK_GB.
    env, recipe = _azure_env(tmp_path / "unset", GCP_DISK_GB="50")
    result = _build(env, recipe, "azure")
    assert result.returncode == 0, result.stderr
    assert _opt(_one(tmp_path / "unset", "vm create"), "--os-disk-size-gb") == "20"
    assert result.stdout.endswith("disk_gb: 20}\n")
    # A dry run shows the override.
    env, recipe = _azure_env(tmp_path / "dry", AZURE_DISK_GB="64")
    result = _build(env, "--dry-run", recipe, "azure")
    assert result.returncode == 0, result.stderr
    assert " --os-disk-size-gb 64 " in result.stderr and result.stdout.endswith("disk_gb: 64}\n")


def test_azure_reboot_uses_vm_restart(tmp_path):
    env, recipe = _azure_env(tmp_path)
    _fast_sleep(tmp_path)
    env.update(FAKE_SSH_FAIL_ON="setup.sh", FAKE_SSH_FAIL_RC="100", FAKE_BOOT_FLIP_AFTER="2")
    result = _build(env, recipe, "azure")
    assert result.returncode == 0, result.stderr
    restart = _one(tmp_path, "vm restart")
    run_id = _run_id(tmp_path)
    assert _opt(restart, "--resource-group") == run_id and _opt(restart, "--name") == run_id
    timeline = _timeline(tmp_path)
    assert not any("systemctl reboot" in c for _b, c in timeline)
    # Verify ran only once the core saw the new boot.
    assert [b for b, c in timeline if "verify.sh" in c] == ["boot-2"], timeline

    result = _build(env, "--dry-run", recipe, "azure")
    assert result.returncode == 0, result.stderr
    assert "the core calls backend_reboot (azure)" in result.stderr


def test_azure_dry_run_runs_no_az(tmp_path):
    for os_family in ("linux", "windows"):
        env, recipe = _azure_env(tmp_path / os_family, os_family)
        result = _build(env, "--dry-run", recipe, "azure")
        assert result.returncode == 0, result.stderr
        assert _az(tmp_path / os_family) == []
        prefix = f"example-lab-{os_family}"
        for printed in (f"DRY-RUN: az vm image show --subscription {SUB} --location {LOCATION} --urn ",
                        f"DRY-RUN: az rest --method put --url /subscriptions/{SUB}/resourcegroups/ib-",
                        f"DRY-RUN: az vm create --subscription {SUB} --resource-group ib-",
                        "DRY-RUN: az vm deallocate ",
                        f"DRY-RUN: az sig image-definition create --subscription {SUB} --resource-group {GALLERY_RG}",
                        f"DRY-RUN: az sig image-version create --subscription {SUB} --resource-group {GALLERY_RG}",
                        f"DRY-RUN: az group delete --subscription {SUB} --name ib-"):
            assert printed in result.stderr, (os_family, printed)
        assert ("DRY-RUN: az vm run-command invoke" in result.stderr) == (os_family == "windows")
        last = result.stdout.rstrip("\n").splitlines()[-1]
        assert re.fullmatch(rf"azure: \{{image: /subscriptions/{SUB}/resourceGroups/{GALLERY_RG}/providers/"
                            rf"Microsoft.Compute/galleries/{GALLERY}/images/{prefix}/versions/"
                            r"[0-9]+\.[0-9]+\.[0-9]+, vm_size: Standard_B2s, disk_gb: 20\}", last), last
        assert KEY_MATERIAL not in result.stderr + result.stdout


def test_azure_config_checked(tmp_path):
    for i, (cfg, named) in enumerate((
        ({"AZ_SUBSCRIPTION_ID": None}, "AZ_SUBSCRIPTION_ID"),
        ({"AZ_SUBSCRIPTION_ID": "not-a-guid"}, "AZ_SUBSCRIPTION_ID"),
        ({"AZ_BUILD_LOCATION": ""}, "AZ_BUILD_LOCATION"),
        ({"AZ_BUILD_SUBNET_ID": None}, "AZ_BUILD_SUBNET_ID"),
        ({"AZ_BUILD_SUBNET_ID": "build-subnet"}, "AZ_BUILD_SUBNET_ID"),
        ({"AZ_BUILD_SUBNET_ID": SUBNET_ID.replace(SUB, OTHER_SUB)}, "AZ_BUILD_SUBNET_ID"),
        ({"AZ_GALLERY_RG": None}, "AZ_GALLERY_RG"),
        ({"AZ_GALLERY": None}, "AZ_GALLERY"),
        ({"AZ_GALLERY": "has-hyphen"}, "AZ_GALLERY"),
    )):
        env, recipe = _azure_env(tmp_path / f"c{i}", config=cfg)
        result = _build(env, "--dry-run", recipe, "azure")
        assert result.returncode == 2, (cfg, result.stderr)
        assert named in result.stderr, (cfg, result.stderr)
        # --reap checks the config too.
        result = _build(env, "--reap", "azure")
        assert result.returncode == 2, (cfg, result.stderr)
        assert _az(tmp_path / f"c{i}") == [], cfg

    # The recipe must set AZURE_VM_SIZE (and BASE_AZURE) to build on Azure,
    # and only on Azure.
    for key in ("AZURE_VM_SIZE", "BASE_AZURE"):
        env, recipe = _azure_env(tmp_path / key, **{key: None})
        result = _build(env, "--dry-run", recipe, "azure")
        assert result.returncode == 2, (key, result.stderr)
        assert f"does not set {key}" in result.stderr, (key, result.stderr)
        # The same recipe builds on another backend.
        env["IMAGE_BUILD_BACKENDS_DIR"] = str(FAKE_BACKENDS)
        with open(env["IMAGE_BUILD_CONFIG"], "a") as config:
            config.write("FAKE_REGION='test-1'\n")
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 0, (key, result.stderr)
    env, recipe = _azure_env(tmp_path / "size", AZURE_VM_SIZE="Standard B2s")
    result = _build(env, "--dry-run", recipe, "azure")
    assert result.returncode == 2
    assert "AZURE_VM_SIZE" in result.stderr

    # The config may not set the recipe's Azure keys.
    for key in ("AZURE_VM_SIZE", "BASE_AZURE"):
        env, recipe = _azure_env(tmp_path / f"cfg-{key}", config={key: "x"})
        result = _build(env, "--dry-run", recipe, "azure")
        assert result.returncode == 2, key
        assert f"sets {key}, a recipe key" in result.stderr, (key, result.stderr)


def test_azure_check_config_requires_az_outside_dry_run(tmp_path):
    bash = shutil.which("bash")
    (tmp_path / "with").mkdir()
    (tmp_path / "with" / "az").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "with" / "az").chmod(0o755)
    (tmp_path / "without").mkdir()

    def check(dry_run, path_dir):
        script = (f"set -euo pipefail\nsource {shlex.quote(str(AZURE_BACKEND))}\n"
                  f"AZ_SUBSCRIPTION_ID={SUB} AZ_BUILD_LOCATION={LOCATION} "
                  f"AZ_BUILD_SUBNET_ID={SUBNET_ID} AZ_GALLERY_RG={GALLERY_RG} AZ_GALLERY={GALLERY}\n"
                  f"DRY_RUN={dry_run}\nPATH={shlex.quote(str(path_dir))}\nbackend_check_config\n")
        return subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=30,
                              env={"PATH": os.environ["PATH"]})

    result = check(0, tmp_path / "without")
    assert result.returncode != 0
    assert "(az)" in result.stderr
    assert check(1, tmp_path / "without").returncode == 0
    result = check(0, tmp_path / "with")
    assert result.returncode == 0, result.stderr
