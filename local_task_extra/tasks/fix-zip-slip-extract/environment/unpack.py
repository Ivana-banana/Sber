from pathlib import Path
from zipfile import ZipFile

def unpack(upload: str, destination: str) -> None:
    with ZipFile(upload) as archive:
        archive.extractall(destination)
