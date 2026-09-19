"""POST /explain-version 的端到端测试。

覆盖：基准锁可达性校验（422）、强制版本的反事实求解、
added/removed/changed 分组与 PEP 440 等价语义、409 冲突来源链复用、
包名规范化、marker、预发布、确定性，以及 /resolve 行为不变。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ENV = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}


def c(version, deps=None):
    return {"version": version, "dependencies": deps or []}


def d(name, spec="", marker=""):
    return {"name": name, "specifier": spec, "marker": marker}


def explain(repo, target_package, target_version, *, env=None, **overrides):
    body = {
        "repository": repo,
        "root": "app",
        "target_environment": env or ENV,
        "target_package": target_package,
        "target_version": target_version,
    }
    body.update(overrides)
    return client.post("/explain-version", json=body)


# --------------------------------------------------------------------- #
# 1. 基本反事实：基准锁与 /resolve 一致；强制低版本产生 changed
# --------------------------------------------------------------------- #
def test_explain_baseline_matches_resolve_and_forces_lower_version():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0"), c("2.0")],
    }
    r = explain(repo, "libx", "1.0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    # 基准锁就是 /resolve 的确定性结果（高版本优先）
    assert body["baseline"]["locked"] == {"app": "1.0", "libx": "2.0"}
    assert body["counterfactual"]["locked"] == {"app": "1.0", "libx": "1.0"}
    assert body["diff"] == {
        "added": [],
        "removed": [],
        "changed": [{"name": "libx", "from": "2.0", "to": "1.0"}],
    }

    # 直接对照 /resolve
    r2 = client.post("/resolve", json={
        "repository": repo, "root": "app", "target_environment": ENV,
    })
    assert r2.json()["locked"] == body["baseline"]["locked"]


def test_explain_forced_version_equals_baseline_yields_empty_diff():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0"), c("2.0")],
    }
    r = explain(repo, "libx", "2.0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counterfactual"]["locked"] == body["baseline"]["locked"]
    assert body["diff"] == {"added": [], "removed": [], "changed": []}


def test_explain_higher_version_feasible():
    """强制高于基准的版本：其余规则不变，取该钉版本下的首个完整解。"""
    repo = {
        "app": [c("1.0", [d("libx", "<2.0")])],
        "libx": [c("1.0"), c("2.0")],
    }
    # 基准锁：libx 被 <2.0 限制到 1.0；强制 2.0 与根约束冲突 -> 409
    r = explain(repo, "libx", "2.0")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["subject"] == "libx"

    # 放开约束后强制 2.0 可行
    repo["app"] = [c("1.0", [d("libx")])]
    r2 = explain(repo, "libx", "2.0")
    assert r2.status_code == 200, r2.text
    assert r2.json()["baseline"]["locked"]["libx"] == "2.0"


# --------------------------------------------------------------------- #
# 2. added / removed / changed：不同版本切换传递依赖
# --------------------------------------------------------------------- #
def test_explain_added_removed_changed_grouping():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [
            c("1.0", [d("old-dep")]),
            c("2.0", [d("new-dep")]),
        ],
        "old-dep": [c("1.0")],
        "new-dep": [c("1.0")],
    }
    # 基准锁 libx==2.0、new-dep；强制 1.0 后 old-dep 新增、new-dep 消失
    r = explain(repo, "libx", "1.0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counterfactual"]["locked"] == {
        "app": "1.0", "libx": "1.0", "old-dep": "1.0",
    }
    diff = body["diff"]
    assert diff["added"] == [
        {"name": "old-dep", "from": None, "to": "1.0"}
    ]
    assert diff["removed"] == [
        {"name": "new-dep", "from": "1.0", "to": None}
    ]
    assert diff["changed"] == [
        {"name": "libx", "from": "2.0", "to": "1.0"}
    ]


def test_explain_pep440_equivalent_versions_produce_no_change():
    """强制写 2.0.0 与基准锁定的 2.0 按 PEP 440 等价，diff 为空。"""
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0"), c("2.0")],
    }
    r = explain(repo, "libx", "2.0.0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_version"] == "2.0.0"
    assert body["counterfactual"]["locked"]["libx"] == "2.0"
    assert body["diff"] == {"added": [], "removed": [], "changed": []}


# --------------------------------------------------------------------- #
# 3. 422：目标包在基准锁中不可达 / 非法版本 / 快照非法 / 多余字段
# --------------------------------------------------------------------- #
def test_explain_target_unreachable_422():
    repo = {
        "app": [c("1.0", [d("libx", marker="sys_platform == 'win32'")])],
        "libx": [c("1.0")],
    }
    r = explain(repo, "libx", "1.0")
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "invalid_snapshot"
    assert "libx" in body["message"]
    assert "基准锁" in body["message"]


def test_explain_target_unknown_package_422():
    repo = {"app": [c("1.0")]}
    r = explain(repo, "ghost", "1.0")
    assert r.status_code == 422
    assert "ghost" in r.json()["message"]


def test_explain_invalid_target_version_422():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0")],
    }
    r = explain(repo, "libx", "not-a-version")
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "invalid_snapshot"
    assert "target_version" in body["message"]
    assert "PEP 440" in body["message"]


def test_explain_target_version_not_in_snapshot_is_409_not_422():
    """版本串合法但快照没有该候选：钉约束无解，返回 409 来源链。"""
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0"), c("2.0")],
    }
    r = explain(repo, "libx", "9.9")
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "conflict"
    conflict = body["conflicts"][0]
    assert conflict["subject"] == "libx"
    assert conflict["kind"] == "no_candidates"
    # 钉约束作为根需求出现在来源链中
    requires = {
        link["requires"]
        for chain in conflict["constraints"]
        for link in chain["chain"]
    }
    assert "==9.9" in requires


def test_explain_baseline_unsatisfiable_422():
    """基准锁本身无可行解：无法定义“基准锁中的可达性”，返回 422。"""
    repo = {
        "app": [c("1.0", [d("libx", "==2.0")])],
        "libx": [c("1.0")],
    }
    r = explain(repo, "libx", "1.0")
    assert r.status_code == 422
    assert "基准" in r.json()["message"]


def test_explain_unknown_root_422():
    r = explain({"app": [c("1.0")]}, "app", "1.0", root="missing")
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"


def test_explain_missing_fields_422():
    body = {
        "repository": {"app": [c("1.0")]},
        "root": "app",
        "target_environment": ENV,
        "target_package": "app",
    }
    r = client.post("/explain-version", json=body)
    assert r.status_code == 422


def test_explain_extra_field_forbidden_422():
    repo = {"app": [c("1.0")]}
    r = explain(repo, "app", "1.0", unknown_field=1)
    assert r.status_code == 422


def test_explain_invalid_marker_still_422():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0"), c("2.0", [d("x", marker="os_name == 'posix'")])],
    }
    r = explain(repo, "libx", "1.0")
    assert r.status_code == 422
    assert "os_name" in r.json()["message"]


# --------------------------------------------------------------------- #
# 4. 409：强制版本与传递约束冲突，来源链复用 /resolve 结构
# --------------------------------------------------------------------- #
def test_explain_forced_version_conflicts_with_transitive_constraints_409():
    repo = {
        "app": [c("1.0", [d("libx"), d("liby")])],
        "libx": [
            c("1.0"),
            c("2.0", [d("liby", ">=2.0")]),
        ],
        "liby": [c("1.0"), c("2.0")],
    }
    # 强制 libx==1.0 可行；反事实走普通 /resolve 规则（不做变更最小化），
    # 无约束的 liby 仍取最高的 2.0。
    r_ok = explain(repo, "libx", "1.0")
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["counterfactual"]["locked"] == {
        "app": "1.0", "libx": "1.0", "liby": "2.0",
    }

    # app 钉死 liby==1.0：强制 libx==2.0 要求 liby>=2.0，互斥 -> 409
    repo2 = {
        "app": [c("1.0", [d("libx"), d("liby", "==1.0")])],
        "libx": [c("1.0"), c("2.0", [d("liby", ">=2.0")])],
        "liby": [c("1.0"), c("2.0")],
    }
    r = explain(repo2, "libx", "2.0")
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "conflict"
    conflict = body["conflicts"][0]
    assert conflict["subject"] == "liby"
    # 所有来源链都从根需求（package=null）开始；钉约束 ==2.0 在链中可见
    pin_seen = False
    for entry in conflict["constraints"]:
        assert entry["chain"][0]["package"] is None
        for link in entry["chain"]:
            if link["requires"] == "==2.0" and link["package"] is None:
                pin_seen = True
    assert pin_seen
    assert conflict["partial_selection"]


def test_explain_incompatible_kind_conflict_chain():
    """环上已选版本与回边约束不兼容：kind=incompatible。"""
    repo = {
        "app": [c("1.0", [d("libx", "==1.0")])],
        # libx 1.0 与 shared 1.0 构成依赖环；shared 2.0 的回边要求 libx==2.0
        "libx": [
            c("1.0", [d("shared")]),
            c("2.0", [d("shared")]),
        ],
        "shared": [
            c("1.0", [d("libx", "==1.0")]),
            c("2.0", [d("libx", "==2.0")]),
        ],
    }
    # 基准可解：app 钉 libx==1.0 -> shared==1.0。强制 shared==2.0：
    # libx 已选 1.0，shared 2.0 的环回边要求 libx==2.0 -> incompatible
    r = explain(repo, "shared", "2.0")
    assert r.status_code == 409
    body = r.json()
    conflict = next(c for c in body["conflicts"] if c["kind"] == "incompatible")
    assert conflict["subject"] == "libx"


# --------------------------------------------------------------------- #
# 5. 规范化：target_package 与输出全部用规范化名
# --------------------------------------------------------------------- #
def test_explain_package_name_canonicalized():
    repo = {
        "app": [c("1.0", [d("Lib_X")])],
        "lib-x": [c("1.0"), c("2.0")],
    }
    r = explain(repo, "LIB_X", "1.0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_package"] == "lib-x"
    assert body["diff"]["changed"] == [
        {"name": "lib-x", "from": "2.0", "to": "1.0"}
    ]
    assert body["baseline"]["locked"]["lib-x"] == "2.0"
    assert body["counterfactual"]["locked"]["lib-x"] == "1.0"


# --------------------------------------------------------------------- #
# 6. marker：反事实求解与 /resolve 完全同一套激活语义
# --------------------------------------------------------------------- #
def test_explain_respects_markers():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [
            c("1.0"),
            c("2.0", [
                d("win-lib", marker="sys_platform == 'win32'"),
                d("posix-lib", marker="sys_platform == 'linux'"),
            ]),
        ],
        "win-lib": [c("1.0")],
        "posix-lib": [c("1.0")],
    }
    # Linux 基准：libx==2.0 + posix-lib；强制 libx==1.0 后 posix-lib 移除
    r = explain(repo, "libx", "1.0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseline"]["locked"] == {
        "app": "1.0", "libx": "2.0", "posix-lib": "1.0",
    }
    assert body["counterfactual"]["locked"] == {"app": "1.0", "libx": "1.0"}
    assert body["diff"]["removed"] == [
        {"name": "posix-lib", "from": "1.0", "to": None}
    ]

    # Windows：同一强制版本，差异对象换成 win-lib
    win_env = {"python_version": "3.11", "sys_platform": "win32",
               "platform_machine": "AMD64"}
    r2 = explain(repo, "libx", "1.0", env=win_env)
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["baseline"]["locked"] == {
        "app": "1.0", "libx": "2.0", "win-lib": "1.0",
    }
    assert b2["diff"]["removed"] == [
        {"name": "win-lib", "from": "1.0", "to": None}
    ]


def test_explain_inactive_marker_dependency_does_not_block():
    """强制版本只在 win32 拉入的无解依赖，linux 请求下不影响可行性。"""
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [
            c("1.0"),
            c("2.0", [d("ghost", ">=9", marker="sys_platform == 'win32'")]),
        ],
    }
    r = explain(repo, "libx", "2.0")
    assert r.status_code == 200, r.text
    assert r.json()["counterfactual"]["locked"] == {
        "app": "1.0", "libx": "2.0",
    }


# --------------------------------------------------------------------- #
# 7. 预发布：默认排除；钉住预发布版本按 pip 跟随语义接受（与 /resolve 一致）
# --------------------------------------------------------------------- #
def test_explain_prerelease_pin_follows_pip_semantics():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0"), c("2.0rc1")],
    }
    # 基准（默认不放开预发布）：libx==1.0；显式 ==2.0rc1 谓词携带
    # prereleases=True，钉约束可满足（pip 跟随语义，与 /resolve 一致）。
    r = explain(repo, "libx", "2.0rc1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseline"]["locked"]["libx"] == "1.0"
    assert body["counterfactual"]["locked"]["libx"] == "2.0rc1"
    assert body["diff"]["changed"] == [
        {"name": "libx", "from": "1.0", "to": "2.0rc1"}
    ]


def test_explain_prerelease_toggle_governs_other_packages():
    """目标钉为稳定版，但其传递依赖只有预发布：开关语义仍与 /resolve 相同。"""
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [
            c("1.0"),
            c("2.0", [d("liby")]),
        ],
        "liby": [c("1.0rc1")],
    }
    r = explain(repo, "libx", "2.0")
    assert r.status_code == 409  # liby 只有预发布且未放开
    r2 = explain(repo, "libx", "2.0", allow_prereleases=True)
    assert r2.status_code == 200, r2.text
    assert r2.json()["counterfactual"]["locked"]["liby"] == "1.0rc1"


# --------------------------------------------------------------------- #
# 8. 根包作为反事实目标
# --------------------------------------------------------------------- #
def test_explain_root_package_as_target():
    repo = {
        "app": [c("1.0"), c("2.0", [d("liba")])],
        "liba": [c("1.0")],
    }
    r = explain(repo, "app", "1.0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseline"]["root_version"] == "2.0"
    assert body["counterfactual"]["root_version"] == "1.0"
    assert body["counterfactual"]["locked"] == {"app": "1.0"}
    assert body["diff"] == {
        "added": [],
        "removed": [{"name": "liba", "from": "1.0", "to": None}],
        "changed": [{"name": "app", "from": "2.0", "to": "1.0"}],
    }


def test_explain_root_target_version_violates_root_specifier_409():
    repo = {
        "app": [c("1.0"), c("2.0")],
    }
    r = explain(repo, "app", "1.0", specifier=">=2.0")
    assert r.status_code == 409
    conflict = r.json()["conflicts"][0]
    assert conflict["subject"] == "app"


# --------------------------------------------------------------------- #
# 9. 确定性与 /resolve 不受影响
# --------------------------------------------------------------------- #
def test_explain_deterministic_across_repeated_calls():
    repo = {
        "app": [c("1.0", [d("libx"), d("liby")])],
        "libx": [
            c("1.0", [d("shared", "<2.0")]),
            c("2.0", [d("shared", ">=1.0")]),
        ],
        "liby": [c("1.0", [d("shared")])],
        "shared": [c("1.0"), c("2.0")],
    }
    b1 = explain(repo, "libx", "1.0").json()
    b2 = explain(repo, "libx", "1.0").json()
    assert b1 == b2


def test_explain_does_not_change_resolve_behavior():
    repo = {
        "app": [c("1.0", [d("libx"), d("liby")])],
        "libx": [c("1.0"), c("2.0")],
        "liby": [c("1.0"), c("2.0")],
    }
    resolve_body = {
        "repository": repo, "root": "app", "target_environment": ENV,
    }
    before = client.post("/resolve", json=resolve_body).json()
    explain(repo, "libx", "1.0")
    explain(repo, "liby", "9.9")  # 409 的反事实也不留下共享状态
    after = client.post("/resolve", json=resolve_body).json()
    assert before == after
    assert before["locked"] == {"app": "1.0", "libx": "2.0", "liby": "2.0"}
