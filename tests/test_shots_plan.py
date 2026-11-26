"""Wan session plan (no network): render order, the two-GPU notebook and its worker."""
import json
import py_compile

from pipeline import kaggle_job
from pipeline.shots import NOTEBOOK_2GPU, WORKER, ordered

WAN = [{"id": f"shot-0{n}-x"} for n in (3, 4, 5, 9)]


def test_order_by_shot_number_and_lanes_alternate():
    order = [s["id"] for s in ordered(WAN, "9,5,4,3")]
    assert order == ["shot-09-x", "shot-05-x", "shot-04-x", "shot-03-x"]
    assert (order[0::2], order[1::2]) == (["shot-09-x", "shot-04-x"], ["shot-05-x", "shot-03-x"])  # GPU 0 / GPU 1
    assert ordered(WAN, None) == WAN


def test_two_gpu_notebook_renders_with_its_own_code_file(tmp_path):
    ref = kaggle_job.render_kernel("wan", NOTEBOOK_2GPU, {"job_id": "x", "config": {}, "jobs": []}, "someone",
                                   "someone/heritage-short-wan-in", {"machine_shape": "NvidiaTeslaT4"}, tmp_path)
    meta = json.loads((tmp_path / "kernel-metadata.json").read_text())
    assert ref == "someone/heritage-short-wan" and meta["code_file"] == NOTEBOOK_2GPU
    assert meta["machine_shape"] == "NvidiaTeslaT4" and (tmp_path / NOTEBOOK_2GPU).exists()
    py_compile.compile(str(WORKER), doraise=True)
