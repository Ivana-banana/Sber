# Archive importer

`/app/unpack.py` extracts customer zip archives. Fix the security defect without adding dependencies. Keep the public function signature `unpack(upload, destination)`. Normal nested members must still extract; entries that resolve outside `destination` must be rejected with `ValueError`.

Do not modify files under `/tests`; they are supplied only to the verifier. The verifier will exercise the changed production file.
