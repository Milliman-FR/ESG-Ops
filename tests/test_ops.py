import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import BadZipFile, ZipFile

from esg_api import extract_table_metadata, parse_project_url
from main import download


class OpsDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.input_directory = root / "input"
        self.input_directory.mkdir()
        self.output_directory = root / "output"

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

        post_response = MagicMock()
        post_response.__enter__.return_value = post_response
        post_response.iter_content.return_value = iter([archive_bytes.getvalue()])
        with (
            patch.dict(os.environ, {"TOKEN": "fake-test-token"}),
            patch("main.load_dotenv"),
            patch("main.requests.get") as get,
            patch("main.requests.post", return_value=post_response) as post,
        ):
            get.side_effect = [MagicMock(**{"json.return_value": states}),
                               MagicMock(**{"json.return_value": ["seed/RN_inputs/data.csv"]})]
            summary = download(self.input_directory, self.output_directory)

        self.assertEqual(summary["versionId"], "v1")
        self.assertEqual(summary["universe"], "RN")
        self.assertEqual(summary["requestedFileCount"], 1)
        self.assertNotIn("fake-test-token", json.dumps(summary))
        self.assertEqual(get.call_args_list[1].kwargs["params"], {"sensitivityId": "sensitivity-1"})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer fake-test-token")
        self.assertEqual(post.call_args.kwargs["json"], {"filePaths": ["seed/RN_inputs/data.csv"]})
        with ZipFile(self.output_directory / "esg_download.zip") as result:
            self.assertEqual(result.namelist(), ["seed/RN_inputs/data.csv"])
        self.assertEqual(
            json.loads((self.output_directory / "download_summary.json").read_text(encoding="utf-8")),
            summary,
        )

    def test_missing_token_fails_before_network(self):
        self.write_inputs(ProjectUrl="https://esg-test.milliman-mind.com/p/p", TableId="t")
        with patch.dict(os.environ, {"TOKEN": ""}), patch("main.load_dotenv"), patch("main.requests.get") as get:
            with self.assertRaisesRegex(ValueError, "TOKEN absent"):
                download(self.input_directory, self.output_directory)
            get.assert_not_called()

    def test_insecure_url_is_rejected(self):
        self.write_inputs(ProjectUrl="http://esg-test.milliman-mind.com/p/p", TableId="t")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            download(self.input_directory, self.output_directory)

    def test_untrusted_host_is_rejected_before_sending_token(self):
        self.write_inputs(ProjectUrl="https://esg-example.invalid/p/p", TableId="t")
        with patch("main.requests.get") as get:
            with self.assertRaisesRegex(ValueError, "milliman-mind.com"):
                download(self.input_directory, self.output_directory)
            get.assert_not_called()

    def test_invalid_archive_is_not_published(self):
        self.write_inputs(ProjectUrl="https://esg-test.milliman-mind.com/p/p", TableId="t")
        states = [{"tableId": "t", "tableName": "Test", "versionId": "v1"}]
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = iter([b"not a ZIP archive"])
        with (
            patch.dict(os.environ, {"TOKEN": "fake-test-token"}),
            patch("main.load_dotenv"),
            patch("main.requests.get") as get,
            patch("main.requests.post", return_value=response),
        ):
            get.side_effect = [MagicMock(**{"json.return_value": states}),
                               MagicMock(**{"json.return_value": ["data.csv"]})]
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


if __name__ == "__main__":
    unittest.main()