#!/usr/bin/env python3
"""Recall@k eval for vault retrieval: realistic KO/EN/synonym queries -> expected note.

Usage: vault-search-eval.py [--k 3] [--backend bm25|<cmd>]
`--backend` other than bm25 is a shell command template with {q} that prints
the vault-relative paths of its ranked hits one per line (used to trial
alternative engines without wiring them in).
"""
import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# (query, expected note stem) — phrased as a user would ask, deliberately not
# copying the router keywords verbatim.
CASES = [
    ("로봇에 ssh 접속이 안 되는데 ping은 돼", "penny_ssh_ufw_port22_drop"),
    ("how do I connect to a robot over iap tunnel", "robot_ssh"),
    ("같은 구역에 다시 들어가면 소리가 안 남", "zone_reentry_no_sound"),
    ("후진음이 멈추지 않고 계속 나와", "ticket_ESE232"),
    ("cloud sound profile overriding the default navigation sound", "sound_profile_overrides_default"),
    ("feature flag 새로 추가하는 방법", "feature_flag_add"),
    ("aarch64 cross compile binary for the robot", "bazel_build"),
    ("multi robot relay crashes with map::at", "multi_robot_relay_crash_loop"),
    ("오브벡 카메라 껐다 켜면 죽는 문제", "orbbec_stream_keepalive"),
    ("gyro bias compensation on the imu", "imu_bias_compensation"),
    ("센서 타임스탬프 동기화 에픽", "ticket_ESE220"),
    ("rust 노드를 ROS1에 붙이는 FFI 방법", "rust_ros1_ffi"),
    ("tray camera usb error -75 overflow", "tray_cam"),
    ("시뮬레이터 로컬에서 띄우기", "penny_sim_local"),
    ("indicator engine에 새로운 입력 추가 예시", "indicator_engine_add_input_example"),
    ("loop sound starts too late after display", "ticket_PS60784"),
    ("sick 라이다 cola2 프로토콜 세션 생성", "sick_cola2_protocol"),
    ("wifi 드라이버가 계속 kill 되는 문제", "iwlwifi_kill_loop"),
    ("push docker image without CI", "docker_push_no_ci"),
    ("엑스큐즈미 후에 존 사운드 재개 안 됨", "excuse_me_pause_resume"),
]


def bm25_hits(query, k):
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "vault-bm25.py"), "query", "--top", "20", query],
        capture_output=True, text=True, check=True,
    )
    # Vault notes only: work-notes (incl. this eval's own report) would otherwise self-match.
    return [h["path"] for h in json.loads(out.stdout) if h["source"] == "vault"][:k]


def cmd_hits(template, query, k):
    out = subprocess.run(template.format(q=shlex.quote(query)), shell=True, capture_output=True, text=True, check=True)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()][:k]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--backend", default="bm25")
    args = ap.parse_args()
    hits_total = 0
    print("| # | query | expected | hit@k | top-k |\n|---|---|---|---|---|")
    for i, (query, expected) in enumerate(CASES, 1):
        hits = bm25_hits(query, args.k) if args.backend == "bm25" else cmd_hits(args.backend, query, args.k)
        ok = any(Path(h).stem == expected for h in hits)
        hits_total += ok
        print(f"| {i} | {query} | {expected} | {'✅' if ok else '❌'} | {', '.join(Path(h).stem for h in hits)} |")
    print(f"\nrecall@{args.k} = {hits_total}/{len(CASES)} = {hits_total / len(CASES):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
