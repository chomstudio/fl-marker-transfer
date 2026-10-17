"""FL Studioの代替APIで入力検査・置換・復元を検証する。"""

import copy
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "Pattern Marker Transfer.pyscript"


class Marker:
    """公開プロパティを持つマーカー。"""

    def __init__(self, time=0, name="", mode=0):
        self.time = time
        self.name = name
        self.mode = mode
        self.tsnum = 7
        self.tsden = 8
        self.scale_root = 2
        self.scale_helper = "0,1,0,0,1,0,1,0,0,1,0,1"


class Score:
    """順序変更、重複統合、部分失敗を模擬し、変更APIの呼び出しを追跡する。"""

    PPQ = 96
    tsnum = 4
    tsden = 4

    def __init__(self, markers):
        self.markers = copy.deepcopy(list(markers))
        self.notes = [{"time": 120, "number": 60, "selected": True, "length": 96}]
        self.calls = []
        self.fail_add = 0
        self.fail_delete = 0
        self.fail_read = 0
        self.merge = False
        self.corrupt_other = False

    @property
    def markerCount(self):
        return len(self.markers)

    def getMarker(self, index):
        if self.fail_read:
            self.fail_read -= 1
            raise RuntimeError("read failed")
        return self.markers[index]

    def deleteMarker(self, index):
        self.calls.append(("delete", index))
        if self.fail_delete:
            self.fail_delete -= 1
            raise RuntimeError("delete failed")
        del self.markers[index]

    def addMarker(self, marker):
        self.calls.append(("add", marker.time))
        if self.fail_add:
            self.fail_add -= 1
            raise RuntimeError("add failed")
        if self.merge:
            self.markers = [m for m in self.markers if m.time != marker.time]
        self.markers.append(copy.deepcopy(marker))
        self.markers.sort(key=lambda m: m.time)
        if self.corrupt_other:
            for value in self.markers:
                if value.mode != 0:
                    value.tsnum = 99


class Dialog:
    """入力欄を記録する。ホストのキャンセル機構は再現しない。"""

    def __init__(self, title, description):
        self.title = title
        self.description = description
        self.values = {}

    def addInputText(self, name, value):
        self.values[name] = value

    def addInputKnobInt(self, name, value, minimum, maximum):
        self.values[name] = value
        self.knob_range = (minimum, maximum)

    def getInputValue(self, name):
        return self.values[name]


def load(markers=()):
    """実ファイルを毎回独立した起動として読み込む。"""
    score = Score(markers)
    logs = []
    messages = []
    api = types.SimpleNamespace(
        score=score,
        Marker=Marker,
        ScriptDialog=Dialog,
        Utils=types.SimpleNamespace(log=logs.append, ShowMessage=messages.append),
    )
    namespace = {}
    with patch.dict(sys.modules, {"flpianoroll": api}):
        exec(  # noqa: S102 - 対象のローカルスクリプトだけを代替APIで実行
            compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), namespace
        )
    return namespace, score, logs, messages


def payload(rows=None, **changes):
    """正式データを作り、検査対象の値だけ上書きする。"""
    data = {"format": "PMT-1", "ppq": 96, "mode": 0, "markers": rows or []}
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


class TransferTests(unittest.TestCase):
    """データの契約と、プレビューの遷移・異常時の安全性を検査する。"""

    def setUp(self):
        self.initial = [
            Marker(100, "旧"),
            Marker(300, "拍子", 8),
            Marker(500, "音階", 12),
        ]
        self.ns, self.score, self.logs, self.messages = load(self.initial)
        self.form = self.ns["createDialog"]()

    def apply_text(self, text):
        self.form.values[self.ns["DATA"]] = text
        self.ns["apply"](self.form)

    def normals(self):
        return [(m.time, m.name) for m in self.score.markers if m.mode == 0]

    def test_dialog_current_value_and_fresh_identity(self):
        self.assertEqual(len(self.form.values), 2)
        self.assertEqual(self.form.values[self.ns["OFFSET"]], 0)
        self.assertEqual(self.form.knob_range, (-16, 16))
        self.assertEqual(
            json.loads(next(iter(self.form.values.values())))["format"], "PMT-1"
        )
        other, _, _, _ = load([Marker(25, "別パターン")])
        self.assertNotEqual(self.ns["DATA"], other["DATA"])
        self.assertNotEqual(self.ns["OFFSET"], other["OFFSET"])
        self.assertEqual(
            json.loads(other["_INITIAL_TEXT"])["markers"][0]["name"], "別パターン"
        )

    def test_unchanged_is_noop(self):
        self.apply_text(self.ns["_INITIAL_TEXT"])
        self.assertEqual(self.score.calls, [])

    def test_repeated_apply_no_accumulation_and_properties_preserved(self):
        protected = [vars(m).copy() for m in self.score.markers if m.mode]
        notes = copy.deepcopy(self.score.notes)
        text = payload([{"time": 20, "name": "新"}, {"time": 40, "name": "終"}])
        self.apply_text(text)
        calls = len(self.score.calls)
        self.apply_text(text)
        self.assertEqual(len(self.score.calls), calls)
        self.assertEqual(self.normals(), [(20, "新"), (40, "終")])
        self.assertEqual(protected, [vars(m) for m in self.score.markers if m.mode])
        self.assertEqual(self.score.notes, notes)

    def test_valid_to_invalid_blank_and_original_restore(self):
        for final in ("{", "", " \n\t", self.ns["_INITIAL_TEXT"], payload(ppq=48)):
            with self.subTest(final=final):
                self.apply_text(payload([{"time": 12, "name": "変更"}]))
                self.apply_text(final)
                self.assertEqual(self.normals(), [(100, "旧")])

    def test_empty_array_deletes_only_normals(self):
        self.apply_text(payload())
        self.assertEqual(self.normals(), [])
        self.assertEqual([m.mode for m in self.score.markers], [8, 12])
        self.apply_text("")
        self.assertEqual(self.normals(), [(100, "旧")])

    def test_empty_startup(self):
        namespace, score, _, _ = load()
        self.assertEqual(json.loads(namespace["_INITIAL_TEXT"])["markers"], [])
        form = namespace["createDialog"]()
        form.values[namespace["DATA"]] = payload([{"time": 0, "name": "追加"}])
        namespace["apply"](form)
        form.values[namespace["DATA"]] = "invalid"
        namespace["apply"](form)
        self.assertEqual(score.markers, [])

    def test_unicode_spaces_escapes_and_long_labels(self):
        name = '  日本語😀 "quote" \\ \n\t' + "長" * 5000 + "  "
        text = payload([{"time": 0, "name": name}])
        self.apply_text(text)
        self.assertEqual(self.normals(), [(0, name)])
        encoded = self.ns["_encode"](self.ns["_normal"](self.ns["_snapshot"]()), 96)
        self.assertNotIn("\n", encoded)
        self.assertIn("日本語😀", encoded)
        self.assertEqual(json.loads(encoded)["markers"][0]["name"], name)

    def test_output_stable_time_sort_and_duplicates(self):
        rows = [
            {"time": 5, "name": "B"},
            {"time": 0, "name": "A"},
            {"time": 5, "name": "C"},
        ]
        self.assertEqual(
            json.loads(self.ns["_encode"](rows, 96))["markers"],
            [rows[1], rows[0], rows[2]],
        )
        self.apply_text(payload(rows + [rows[0]]))
        self.assertEqual(self.normals(), [(0, "A"), (5, "B"), (5, "C"), (5, "B")])

    def test_legacy_format_and_empty_name(self):
        self.apply_text(
            payload([{"time": 2147483647, "name": ""}], format="PMT-preflight-1")
        )
        self.assertEqual(self.normals(), [(2147483647, "")])

    def test_bad_envelopes(self):
        bad = ["null", "[]", "{}", "true", "{", '{"format":"PMT-1","format":"PMT-1"}']
        for change in (
            {"format": "PMT-2"},
            {"format": []},
            {"ppq": 0},
            {"ppq": 48},
            {"ppq": True},
            {"ppq": 96.0},
            {"mode": False},
            {"mode": 8},
            {"markers": {}},
            {"extra": 1},
            {"extra": float("nan")},
        ):
            bad.append(payload(**change))
        for key in ("format", "ppq", "mode", "markers"):
            data = json.loads(payload())
            del data[key]
            bad.append(json.dumps(data))
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises((ValueError, TypeError)):
                    self.ns["_decode"](text, 96)
                self.apply_text(text)
                self.assertEqual(self.score.calls, [])

    def test_bad_rows_reject_all_before_delete(self):
        bad_rows = [None, [], {}, {"time": 0}, {"time": 0, "name": "x", "extra": 1}]
        bad_rows += [
            {"time": value, "name": "x"} for value in (-1, 2147483648, True, 1.0, "0")
        ]
        bad_rows += [{"time": 0, "name": value} for value in (None, 1, True, "a\x00b")]
        for row in bad_rows:
            with self.subTest(row=row):
                self.apply_text(payload([{"time": 1, "name": "有効"}, row]))
                self.assertEqual(self.score.calls, [])
        text = payload([{"time": 1, "name": "x"}]).replace(
            '"time": 1', '"time": 1,"time": 2'
        )
        self.apply_text(text)
        self.assertEqual(self.score.calls, [])

    def test_protected_collision_rejected_before_delete(self):
        for time in (300, 500):
            self.apply_text(payload([{"time": time, "name": "衝突"}]))
        self.assertEqual(self.score.calls, [])

    def test_existing_cross_mode_collision_does_not_risk_restore(self):
        ns, score, _, _ = load([Marker(0, "通常"), Marker(0, "拍子", 8)])
        form = ns["createDialog"]()
        form.values[ns["DATA"]] = payload()
        ns["apply"](form)
        self.assertEqual(score.calls, [])

    def test_add_delete_and_read_failure_restore(self):
        for attribute in ("fail_add", "fail_delete", "fail_read"):
            with self.subTest(attribute=attribute):
                setattr(self.score, attribute, 1)
                self.apply_text(payload([{"time": 10, "name": "新"}]))
                self.assertEqual(self.normals(), [(100, "旧")])
                self.assertFalse(self.messages)

    def test_partial_add_failure_restores(self):
        original = self.score.addMarker
        count = 0

        def failing_add(marker):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("second add failed")
            original(marker)

        self.score.addMarker = failing_add
        self.apply_text(payload([{"time": 10, "name": "1"}, {"time": 20, "name": "2"}]))
        self.assertEqual(self.normals(), [(100, "旧")])

    def test_host_merging_duplicates_is_failure_and_restores(self):
        self.score.merge = True
        self.apply_text(payload([{"time": 10, "name": "1"}, {"time": 10, "name": "2"}]))
        self.assertEqual(self.normals(), [(100, "旧")])
        self.assertTrue(any("件数が一致しません" in line for line in self.logs))

    def test_permanent_failure_never_reports_successful_restore(self):
        self.score.fail_add = 10
        self.apply_text(payload([{"time": 10, "name": "新"}]))
        self.assertTrue(any("[復元失敗]" in line for line in self.logs))
        self.assertTrue(any("キャンセル" in line for line in self.logs))
        self.assertFalse(any("確認・復元しました" in line for line in self.logs))

    def test_protected_property_corruption_is_reported_without_deleting_others(self):
        self.score.corrupt_other = True
        self.apply_text(payload([{"time": 10, "name": "新"}]))
        self.assertTrue(any("[復元失敗]" in line for line in self.logs))
        self.assertEqual(
            [call for call in self.score.calls if call[0] == "delete"], [("delete", 0)]
        )

    def test_reverse_delete_order(self):
        ns, score, _, _ = load([Marker(0, "A"), Marker(20, "拍子", 8), Marker(40, "B")])
        ns["_replace"]([], ns["_PROTECTED"])
        self.assertEqual(score.calls, [("delete", 2), ("delete", 0)])

    def test_placement_extension_is_pure_and_range_checked(self):
        rows = [{"time": 20, "name": "A"}, {"time": 10, "name": "B"}]
        before = copy.deepcopy(rows)
        self.assertEqual(
            self.ns["_plan_placement"](rows, 96),
            [{"time": 106, "name": "B"}, {"time": 116, "name": "A"}],
        )
        self.assertEqual(rows, before)
        self.assertEqual(self.ns["_plan_placement"](rows, -21), [])
        for offset in (2147483647, True, 0.5):
            with self.assertRaises(ValueError):
                self.ns["_plan_placement"](rows, offset)

    def test_startup_read_failure_does_not_offer_editing(self):
        with patch.object(Score, "getMarker", side_effect=RuntimeError("unreadable")):
            ns, score, logs, messages = load([Marker()])
        self.assertFalse(ns["_READY"])
        self.assertNotIn("createDialog", ns)
        self.assertEqual(score.calls, [])
        self.assertEqual(len(messages), 1)
        self.assertTrue(any("[起動失敗]" in line for line in logs))

    def test_preview_with_host_reset_between_calls(self):
        for text in (payload([{"time": 10, "name": "新"}]), "invalid", ""):
            self.score.markers = copy.deepcopy(self.initial)
            self.apply_text(text)
            expected = [(10, "新")] if text.startswith("{") else [(100, "旧")]
            self.assertEqual(self.normals(), expected)

    def test_marker_preparation_failure_happens_before_delete(self):
        with patch.object(
            self.ns["flp"], "Marker", side_effect=RuntimeError("new failed")
        ):
            self.apply_text(payload([{"time": 10, "name": "新"}]))
        self.assertEqual(self.score.calls, [])
        self.assertEqual(self.normals(), [(100, "旧")])

    def test_readback_failure_restores(self):
        original = self.score.addMarker

        def add_and_fail_read(marker):
            original(marker)
            self.score.fail_read = 1

        self.score.addMarker = add_and_fail_read
        self.apply_text(payload([{"time": 10, "name": "新"}]))
        # 復元の読み戻しも失敗したため、成功とは記録しない。
        self.assertEqual(self.normals(), [(100, "旧")])
        self.assertTrue(any("[復元失敗]" in line for line in self.logs))

    def test_unknown_mode_is_protected(self):
        ns, score, _, _ = load([Marker(0, "通常"), Marker(30, "未知", 99)])
        original = vars(score.markers[1]).copy()
        ns["_replace"]([{"time": 10, "name": "新"}], ns["_PROTECTED"])
        self.assertEqual(vars(next(m for m in score.markers if m.mode == 99)), original)

    def test_reordered_protected_markers_are_not_a_change(self):
        self.score.markers.reverse()
        self.apply_text(payload([{"time": 10, "name": "新"}]))
        self.assertEqual(self.normals(), [(10, "新")])

    def test_non_text_input_restores(self):
        self.apply_text(payload([{"time": 10, "name": "新"}]))
        self.apply_text(None)
        self.assertEqual(self.normals(), [(100, "旧")])

    def test_positive_bar_offset(self):
        self.form.values[self.ns["OFFSET"]] = 1
        self.apply_text(payload([{"time": 0, "name": "A"}, {"time": 100, "name": "B"}]))
        self.assertEqual(self.normals(), [(384, "A"), (484, "B")])

    def test_offset_endpoints_and_outside_range(self):
        for offset, time, expected in ((16, 0, 6144), (-16, 6200, 56)):
            self.form.values[self.ns["OFFSET"]] = offset
            self.apply_text(payload([{"time": time, "name": "A"}]))
            self.assertEqual(self.normals(), [(expected, "A")])
        for offset in (-17, 17, -32, 32):
            self.form.values[self.ns["OFFSET"]] = offset
            self.apply_text(payload([{"time": 0, "name": "拒否"}]))
            self.assertEqual(self.normals(), [(100, "旧")])

    def test_negative_bar_offset_crops_before_boundary(self):
        self.form.values[self.ns["OFFSET"]] = -2
        self.apply_text(
            payload(
                [
                    {"time": 0, "name": "削除"},
                    {"time": 767, "name": "直前"},
                    {"time": 768, "name": "境界"},
                    {"time": 800, "name": "残す"},
                ]
            )
        )
        self.assertEqual(self.normals(), [(0, "境界"), (32, "残す")])

    def test_all_cropped_is_successful_noop(self):
        self.form.values[self.ns["OFFSET"]] = -2
        self.apply_text(payload([{"time": 767, "name": "除外"}]))
        self.assertEqual(self.score.calls, [])
        self.assertEqual(self.normals(), [(100, "旧")])
        self.assertTrue(any("[変更なし]" in line for line in self.logs))
        self.assertFalse(
            any("失敗" in line or "適用中止" in line for line in self.logs)
        )

    def test_all_cropped_after_preview_restores_initial(self):
        text = payload([{"time": 0, "name": "A"}])
        self.form.values[self.ns["OFFSET"]] = 1
        self.apply_text(text)
        self.assertEqual(self.normals(), [(384, "A")])
        self.form.values[self.ns["OFFSET"]] = -2
        self.apply_text(text)
        self.assertEqual(self.normals(), [(100, "旧")])
        self.assertFalse(any("失敗" in line for line in self.logs))

    def test_empty_array_explicitly_deletes_with_any_offset(self):
        for offset in (-2, 1):
            self.score.markers = copy.deepcopy(self.initial)
            self.form.values[self.ns["OFFSET"]] = offset
            self.apply_text(payload())
            self.assertEqual(self.normals(), [])

    def test_offset_changes_do_not_accumulate(self):
        text = payload([{"time": 768, "name": "A"}])
        for offset in (1, 2, -2, 0, 1, 1):
            self.form.values[self.ns["OFFSET"]] = offset
            self.apply_text(text)
            self.assertEqual(self.normals(), [(768 + offset * 384, "A")])

    def test_offset_overflow_and_invalid_values_restore(self):
        for offset in (1, True, 1.0, "1", -17, 17):
            self.form.values[self.ns["OFFSET"]] = offset
            self.apply_text(payload([{"time": 2147483647, "name": "範囲外"}]))
            self.assertEqual(self.normals(), [(100, "旧")])
        self.assertEqual(self.score.calls, [])

    def test_offset_collision_is_checked_after_moving(self):
        self.form.values[self.ns["OFFSET"]] = -1
        self.apply_text(payload([{"time": 684, "name": "拍子と衝突"}]))
        self.assertEqual(self.score.calls, [])

    def test_bar_length_uses_destination_base_signature(self):
        for numerator, denominator, ticks in ((3, 4, 288), (6, 8, 288), (7, 8, 336)):
            with self.subTest(signature=(numerator, denominator)):
                with (
                    patch.object(Score, "tsnum", numerator),
                    patch.object(Score, "tsden", denominator),
                ):
                    ns, score, _, _ = load([Marker(300, "拍子変更", 8)])
                form = ns["createDialog"]()
                form.values[ns["OFFSET"]] = 1
                form.values[ns["DATA"]] = payload([{"time": 0, "name": "A"}])
                ns["apply"](form)
                self.assertEqual(ns["_BAR_TICKS"], ticks)
                self.assertEqual(
                    [(m.time, m.name) for m in score.markers if m.mode == 0],
                    [(ticks, "A")],
                )

    def test_invalid_signature_is_rejected_without_rounding(self):
        for numerator, denominator in ((0, 4), (4, 0), (True, 4), (4, 4.0), (1, 7)):
            with self.assertRaises(ValueError):
                self.ns["_bar_ticks"](96, numerator, denominator)


if __name__ == "__main__":
    unittest.main()
