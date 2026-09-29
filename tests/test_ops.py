import io
import json
import re
import tempfile
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import BadZipFile, ZipFile

import requests

from esg_api import extract_table_metadata, parse_project_url
from main import download, extract_tables, main


class OpsDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.input_directory = root / "input"
        self.input_directory.mkdir()
        self.output_directory = root / "output"
        self.sto_csv = root / "ESG_311225_Central_VA_sto.csv"
        self.sto_csv.write_bytes(b"SIMULATION;ECONOMY;2025\n1;EUR;0.02\n")
        sto_patch = patch("main.STO_CSV", self.sto_csv)
        sto_patch.start()
        self.addCleanup(sto_patch.stop)

    def write_inputs(self, **values):
        (self.input_directory / "inputs.json").write_text(
            json.dumps({"inputs": [
                {"category": "parameters", "name": name, "value": value}
                for name, value in values.items()
            ]}),
            encoding="utf-8",
        )

    def test_download_uses_token_and_produces_zip_and_summary(self):
        self.write_inputs(
            ProjectUrl="https://esg-test.milliman-mind.com/p/project-1/t",
            Token="fake-test-token",
            TableId="table-1",
            SensitivityId="sensitivity-1",
        )
        states = [
            {"tableId": "table-1", "tableName": "Test", "versionId": "v1",
             "lastOperationStatus": 2, "universe": "RiskNeutral"}
        ]
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("seed/RN_inputs/data.csv", "a,b\n1,2\n")
            archive.writestr("MLM_Data_MLM_Param_311225/RN_outputs/Tables/table.fac", b"table data")
            archive.writestr("MLM_Data_MLM_Param_311225/RN_outputs/Tables/CR_TRANS_MATRIX.fac", b"matrix")
            archive.writestr("MLM_Data_MLM_Param_311225/RN_outputs/Other/ignored.fac", b"ignored")

        post_response = MagicMock()
        post_response.__enter__.return_value = post_response
        post_response.iter_content.return_value = iter([archive_bytes.getvalue()])
        with patch("main.requests.Session") as session_factory:
            session = session_factory.return_value.__enter__.return_value
            session.get.side_effect = [MagicMock(**{"json.return_value": states}),
                                       MagicMock(**{"json.return_value": ["seed/RN_inputs/data.csv"]})]
            session.post.return_value = post_response
            output = io.StringIO()
            with redirect_stdout(output):
                summary = download(self.input_directory, self.output_directory)

        self.assertEqual(summary["versionId"], "v1")
        self.assertEqual(summary["universe"], "RN")
        self.assertEqual(summary["requestedFileCount"], 1)
        self.assertEqual(summary["tables"], ["tables/table.fac", "tables/CR_TRANS_MATRIX.fac"])
        self.assertNotIn("fake-test-token", json.dumps(summary))
        self.assertNotIn("fake-test-token", output.getvalue())
        self.assertNotIn(self.sto_csv.name, output.getvalue())
        for step in ("[1/4] GET", "[2/4] GET", "[3/4] POST", "[4/4] Extraction"):
            self.assertIn(step, output.getvalue())
        self.assertIn("sensitivity-1", output.getvalue())
        self.assertEqual(session.get.call_args_list[1].kwargs["params"], {"sensitivityId": "sensitivity-1"})
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer fake-test-token")
        self.assertEqual(session.post.call_args.kwargs["json"], {"filePaths": ["seed/RN_inputs/data.csv"]})
        session_factory.assert_called_once_with()
        with ZipFile(self.output_directory / "esg_download.zip") as result:
            self.assertIn("seed/RN_inputs/data.csv", result.namelist())
        self.assertEqual((self.output_directory / "tables/table.fac").read_bytes(), b"table data")
        self.assertEqual((self.output_directory / "tables/CR_TRANS_MATRIX.fac").read_bytes(), b"matrix")
        self.assertFalse((self.output_directory / "tables/ignored.fac").exists())
        self.assertEqual((self.output_directory / self.sto_csv.name).read_bytes(), self.sto_csv.read_bytes())
        self.assertEqual(
            json.loads((self.output_directory / "download_summary.json").read_text(encoding="utf-8")),
            summary,
        )

    def test_missing_token_fails_before_network(self):
        self.write_inputs(ProjectUrl="https://esg-test.milliman-mind.com/p/p", TableId="t")
        with patch("main.requests.Session") as session_factory:
            with self.assertRaisesRegex(ValueError, "Token"):
                download(self.input_directory, self.output_directory)
            session_factory.assert_not_called()

    def test_lfs_pointer_is_rejected_before_network(self):
        self.write_inputs(ProjectUrl="https://esg-test.milliman-mind.com/p/p",
                          Token="fake-test-token", TableId="t")
        self.sto_csv.write_bytes(b"version https://git-lfs.github.com/spec/v1\n" + b"oid sha256:" + b"0" * 64)
        with patch("main.requests.Session") as session_factory:
            with self.assertRaisesRegex(ValueError, "Git LFS non matérialisé"):
                download(self.input_directory, self.output_directory)
            session_factory.assert_not_called()

    def test_insecure_url_is_rejected(self):
        self.write_inputs(ProjectUrl="http://esg-test.milliman-mind.com/p/p", Token="fake-test-token", TableId="t")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            download(self.input_directory, self.output_directory)

    def test_untrusted_host_is_rejected_before_sending_token(self):
        self.write_inputs(ProjectUrl="https://esg-example.invalid/p/p", Token="fake-test-token", TableId="t")
        with patch("main.requests.Session") as session_factory:
            with self.assertRaisesRegex(ValueError, "milliman-mind.com"):
                download(self.input_directory, self.output_directory)
            session_factory.assert_not_called()

    def test_invalid_archive_is_not_published(self):
        self.write_inputs(ProjectUrl="https://esg-test.milliman-mind.com/p/p", Token="fake-test-token", TableId="t")
        states = [{"tableId": "t", "tableName": "Test", "versionId": "v1"}]
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = iter([b"not a ZIP archive"])
        with patch("main.requests.Session") as session_factory:
            session = session_factory.return_value.__enter__.return_value
            session.get.side_effect = [MagicMock(**{"json.return_value": states}),
                                       MagicMock(**{"json.return_value": ["data.csv"]})]
            session.post.return_value = response
            with self.assertRaises(BadZipFile):
                download(self.input_directory, self.output_directory)
        self.assertFalse((self.output_directory / "esg_download.zip").exists())
        self.assertFalse((self.output_directory / "download_summary.json").exists())

    def test_metadata_prefers_table_row_over_sensitivity(self):
        rows = [
            {"tableId": "t", "tableName": "Test", "versionId": "old", "sensitivityId": "s"},
            {"tableId": "t", "tableName": "Test", "versionId": "v1"},
        ]
        self.assertEqual(extract_table_metadata(rows)[0]["versionId"], "v1")
        self.assertEqual(parse_project_url("https://esg-test.milliman-mind.com/p/project-1/t"),
                 ("https://esg-test.milliman-mind.com", "project-1"))

    def test_ops_spec_declares_each_table_separately_under_rn_output(self):
        spec_path = Path(__file__).resolve().parents[1] / ".ops/output-specification.json"
        outputs = json.loads(spec_path.read_text(encoding="utf-8"))["outputs"]
        rn_outputs = [item for item in outputs if item["category"] == "RN Output"]
        sto_output = next(item for item in rn_outputs if item["name"] == "ESG_Central_VA_sto")
        self.assertEqual(sto_output["type"], "csv")
        self.assertEqual(sto_output["pattern"], r"ESG_311225_Central_VA_sto\.csv")
        self.assertEqual(sto_output["options"]["delimiter"], ";")
        names = {
            "CR_CURVE_SPREAD_PC", "CR_CURVE_SPREAD_PC_CEV", "CR_TRANS_MATRIX",
            "CR_TRANS_MATRIX_CEV", "table", "table_CEV",
            "ZCB", "ZCB_CEV",
        }
        self.assertEqual({item["name"] for item in rn_outputs}, names | {sto_output["name"]})
        self.assertEqual(len(rn_outputs), len(names) + 1)
        for item in rn_outputs:
            if item is sto_output:
                continue
            self.assertEqual(item["directory"], "tables")
            self.assertEqual(item["type"], "binary")
            if not item["name"].startswith("ZCB"):
                self.assertEqual(item["pattern"], rf"{item['name']}\.fac")
        for basename, expected_output in (
            ("ZCB_MLM_Data_MLM_Param_311225.fac", "ZCB"),
            ("ZCB_MLM_Data_MLM_Param_311225_CEV.fac", "ZCB_CEV"),
            ("ZCB_CAA_Data_CAA_Param_300626.fac", "ZCB"),
            ("ZCB_CAA_Data_CAA_Param_300626_CEV.fac", "ZCB_CEV"),
        ):
            matched = [item["name"] for item in rn_outputs
                       if re.fullmatch(item["pattern"], basename, re.IGNORECASE)]
            self.assertEqual(matched, [expected_output])
        self.assertEqual({item["name"] for item in outputs if item["category"] == "data"},
                         {"ESGArchive", "DownloadSummary"})

    def test_api_logs_mask_caa_but_request_uses_actual_host(self):
        self.write_inputs(ProjectUrl="https://esg-caa.milliman-mind.com/p/project-1/t",
                          Token="fake-test-token", TableId="table-1")
        states = [{"tableId": "table-1", "tableName": "Test", "versionId": "v1"}]
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("data.csv", b"data")
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = iter([archive_bytes.getvalue()])
        with patch("main.requests.Session") as session_factory:
            session = session_factory.return_value.__enter__.return_value
            session.get.side_effect = [MagicMock(**{"json.return_value": states}),
                                       MagicMock(**{"json.return_value": ["data.csv"]})]
            session.post.return_value = response
            output = io.StringIO()
            with redirect_stdout(output):
                download(self.input_directory, self.output_directory)
        self.assertIn("GET https://esg.milliman-mind.com/", output.getvalue())
        self.assertIn("POST https://esg.milliman-mind.com/", output.getvalue())
        self.assertNotIn("esg-caa", output.getvalue())
        self.assertNotIn("fake-test-token", output.getvalue())
        self.assertIn("esg-caa.milliman-mind.com", session.get.call_args_list[0].args[0])
        self.assertIn("esg-caa.milliman-mind.com", session.post.call_args.args[0])

    def test_api_failure_log_masks_caa_hostname(self):
        error = requests.ConnectTimeout("Connection to esg-caa.milliman-mind.com timed out")
        stderr = io.StringIO()
        with patch("sys.argv", ["main.py", "run", str(self.input_directory), str(self.output_directory)]), \
             patch("main.download", side_effect=error), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_status:
                main()
        self.assertEqual(exit_status.exception.code, 1)
        self.assertIn("esg.milliman-mind.com", stderr.getvalue())
        self.assertNotIn("esg-caa", stderr.getvalue())

    def test_no_tables_keeps_zip_without_creating_tables_folder(self):
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("unrelated/data.fac", b"data")
        with ZipFile(io.BytesIO(archive_bytes.getvalue())) as archive:
            self.assertEqual(extract_tables(archive, self.output_directory), [])
        self.assertFalse((self.output_directory / "tables").exists())

    def test_other_table_name_and_date_are_extracted_with_original_filenames(self):
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("CAA_Data_CAA_Param_300626/RN_outputs/Tables/ZCB_CAA_Data_CAA_Param_300626.fac", b"zcb")
            archive.writestr("CAA_Data_CAA_Param_300626/RN_outputs/Tables/ZCB_CAA_Data_CAA_Param_300626_CEV.fac", b"cev")
            archive.writestr("CAA_Data_CAA_Param_300626/RW_outputs/Tables/other.fac", b"excluded")
        with ZipFile(io.BytesIO(archive_bytes.getvalue())) as archive:
            names = extract_tables(archive, self.output_directory)
        self.assertEqual(names, ["ZCB_CAA_Data_CAA_Param_300626.fac", "ZCB_CAA_Data_CAA_Param_300626_CEV.fac"])
        self.assertEqual((self.output_directory / "tables" / names[0]).read_bytes(), b"zcb")
        self.assertEqual((self.output_directory / "tables" / names[1]).read_bytes(), b"cev")
        self.assertFalse((self.output_directory / "tables/other.fac").exists())

    def test_duplicate_table_names_are_rejected_before_writing(self):
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("MLM_Data_MLM_Param_311225/RN_outputs/Tables/table.fac", b"one")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("MLM_Data_MLM_Param_311225/RN_outputs/Tables/table.fac", b"two")
        with ZipFile(io.BytesIO(archive_bytes.getvalue())) as archive:
            with self.assertRaisesRegex(ValueError, "double"):
                extract_tables(archive, self.output_directory)
        self.assertFalse((self.output_directory / "tables").exists())

    def test_unsafe_table_name_cannot_escape_output_directory(self):
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("MLM_Data_MLM_Param_311225/RN_outputs/Tables/bad:name.fac", b"data")
        with ZipFile(io.BytesIO(archive_bytes.getvalue())) as archive:
            with self.assertRaisesRegex(ValueError, "non sûr"):
                extract_tables(archive, self.output_directory)
        self.assertFalse((self.output_directory / "outside.fac").exists())

    def test_nested_table_entry_is_not_extracted(self):
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            archive.writestr("MLM_Data_MLM_Param_311225/RN_outputs/Tables/../outside.fac", b"data")
        with ZipFile(io.BytesIO(archive_bytes.getvalue())) as archive:
            self.assertEqual(extract_tables(archive, self.output_directory), [])
        self.assertFalse((self.output_directory / "outside.fac").exists())


if __name__ == "__main__":
    unittest.main()