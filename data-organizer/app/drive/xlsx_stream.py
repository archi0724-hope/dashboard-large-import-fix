"""Stream XLSX rows with disk-backed shared strings, including large workbooks."""
from __future__ import annotations

import posixpath
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'


def column_number(reference):
    result = 0
    for character in reference:
        if not character.isalpha():
            break
        result = result * 26 + ord(character.upper()) - ord('A') + 1
    if not 1 <= result <= 16384:
        raise ValueError('Invalid spreadsheet column reference.')
    return result - 1


def sheets(path):
    with tempfile.TemporaryDirectory(prefix='drive-strings-') as temporary, zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 5000 or sum(item.file_size for item in archive.infolist()) > 5 * 1024**3:
            raise ValueError('Workbook exceeds the archive entry/expanded-size processing limits.')
        for name in ('xl/workbook.xml','xl/_rels/workbook.xml.rels'):
            if archive.getinfo(name).file_size > 1024**2:
                raise ValueError('Workbook metadata exceeds the processing limit.')
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        relationships = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        targets = {item.attrib['Id']:item.attrib['Target'] for item in relationships if item.attrib.get('TargetMode') != 'External'}
        with closing(sqlite3.connect(Path(temporary) / 'strings.sqlite')) as strings:
            strings.execute('CREATE TABLE strings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
            if 'xl/sharedStrings.xml' in archive.namelist():
                with archive.open('xl/sharedStrings.xml') as source:
                    events = ET.iterparse(source,events=('start','end'))
                    _,root = next(events)
                    batch,number = [],0
                    for event,element in events:
                        if event == 'end' and element.tag == NS+'si':
                            value = ''.join(part.text or '' for part in element.iter(NS+'t'))
                            if len(value) > 128 * 1024:
                                raise ValueError('A spreadsheet cell exceeds the 128 KB processing limit.')
                            batch.append((number,value))
                            number += 1
                            if len(batch) == 1000:
                                strings.executemany('INSERT INTO strings VALUES(?,?)',batch)
                                batch.clear()
                            root.clear()
                    if batch:
                        strings.executemany('INSERT INTO strings VALUES(?,?)',batch)
                strings.commit()

            @lru_cache(maxsize=512)
            def shared(number):
                row = strings.execute('SELECT value FROM strings WHERE id=?',(int(number),)).fetchone()
                if row is None:
                    raise ValueError('Invalid shared-string reference in workbook.')
                return row[0]

            def rows(target):
                with archive.open(target) as source:
                    events = ET.iterparse(source,events=('start','end'))
                    _,root = next(events)
                    sheet_data = None
                    for event,element in events:
                        if event == 'start' and element.tag == NS+'sheetData':
                            sheet_data = element
                        if event != 'end' or element.tag != NS+'row':
                            continue
                        values = []
                        characters = 0
                        for cell in element.findall(NS+'c'):
                            number = column_number(cell.attrib.get('r','A1'))
                            while len(values) <= number:
                                values.append('')
                            raw = cell.findtext(NS+'v','')
                            kind = cell.attrib.get('t','')
                            values[number] = shared(raw) if kind == 's' else ''.join(part.text or '' for part in cell.iter(NS+'t')) if kind == 'inlineStr' else raw
                            characters += len(values[number])
                            if characters > 128 * 1024:
                                raise ValueError('A spreadsheet row exceeds the 128 KB index-row processing limit.')
                        yield {'row':int(element.attrib.get('r','0')),'values':values}
                        if sheet_data is not None:
                            sheet_data.clear()
                        root.clear()

            for sheet in workbook.iter(NS+'sheet'):
                target = targets.get(sheet.attrib.get(REL,''))
                if not target:
                    continue
                target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
                if not target.startswith('xl/') or '..' in target.split('/'):
                    raise ValueError('Unsafe worksheet path.')
                yield sheet.attrib.get('name','Sheet'),rows(target)
