import os
from check_task_size import MAX_REFERENCE_LINES, MAX_TASK_CHARS, too_long


def _road(*tasks):
    return "\n".join(tasks)


def test_flags_a_long_reference_file(tmp_path):
    ref = tmp_path / "ref"
    (ref / "lib").mkdir(parents=True)
    (ref / "lib" / "big.dart").write_text("x\n" * MAX_REFERENCE_LINES)
    (ref / "lib" / "small.dart").write_text("x\n" * 20)
    road = _road("- [ ] Create Big in lib/big.dart — one class. — task gate: flutter test passes",
                 "- [ ] Create Small in lib/small.dart — one class. — task gate: flutter test passes")
    found = too_long(road, str(tmp_path), str(ref))
    assert [f.target for f in found] == ["lib/big.dart"]
    assert f"{MAX_REFERENCE_LINES} lines" in found[0].reasons[0]


def test_flags_long_task_text_without_a_reference(tmp_path):
    long_task = "- [ ] Create Form in lib/form.dart — " + "a" * MAX_TASK_CHARS + " — task gate: flutter test passes"
    found = too_long(_road(long_task, "- [ ] Create Ok in lib/ok.dart — short. — task gate: x passes"), str(tmp_path))
    assert [f.target for f in found] == ["lib/form.dart"]


def test_skips_exact_pair_edits_and_finished_tasks(tmp_path):
    pairs = "- [ ] Edit lib/a.dart — " + "REPLACE `x` WITH `y`. " * 200 + "— task gate: x passes"
    done = "- [x] Create Form in lib/form.dart — " + "a" * MAX_TASK_CHARS
    assert too_long(_road(pairs, done), str(tmp_path)) == []
