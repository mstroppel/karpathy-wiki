"""Write a tiny synthetic XLSX with formula caches using only the Python stdlib."""

# ruff: noqa: E501 -- Keep the small OOXML fixture's namespace URIs readable.

import sys
import zipfile
from pathlib import Path


def write(path: Path, revised: bool = False) -> None:
    value = 4 if revised else 3
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    members = {
        "[Content_Types].xml": """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        "_rels/.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": f'''<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Max Mustermann" sheetId="1" r:id="rId1"/>
<sheet name="HIDDEN-SHEET-SECRET" sheetId="2" state="veryHidden" r:id="rId2"/></sheets>
</workbook>''',
        "xl/_rels/workbook.xml.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>""",
        "xl/worksheets/sheet1.xml": f'''<worksheet xmlns="{namespace}">
<cols><col min="4" max="4" hidden="1"/></cols><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>Max Mustermann</t></is></c>
<c r="B1" t="inlineStr"><is><t>Value</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>Synthetic total 雪</t></is></c>
<c r="B2"><f>1+{value - 1}</f><v>{value}</v></c>
<c r="C2" t="str"><f>"Max Mustermann"</f><v>Max Mustermann</v></c>
<c r="D2" t="inlineStr"><is><t>HIDDEN-COLUMN-SECRET</t></is></c></row>
<row r="3"><c r="B3"><f>B2*2</f></c></row>
<row r="4" hidden="1"><c r="A4" t="inlineStr"><is><t>HIDDEN-ROW-SECRET</t></is></c></row>
</sheetData></worksheet>''',
        "xl/worksheets/sheet2.xml": f'''<worksheet xmlns="{namespace}"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>HIDDEN-VALUE-SECRET</t></is></c></row>
</sheetData></worksheet>''',
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content.encode("utf-8"))


if __name__ == "__main__":
    write(Path(sys.argv[1]), revised=len(sys.argv) > 2)
