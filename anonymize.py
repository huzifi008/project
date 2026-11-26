
import hashlib
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from lxml import etree

SALT = b"change-me-to-strong-fixed-salt"
DB_PATH = "anon_cache.sqlite"
NAMES_FILE = "names.txt"
PRODUCE_FILE = "produce.txt"

NAME_TAGS = {"IdentifiedObject.name", "Name.name", "Person.name", "Organisation.name", "Employee.name"}
DESC_TAGS = {"IdentifiedObject.description", "IdentifiedObject.comment"}
DIGITS_FIELDS = {"taxNumber", "INN", "KPP", "serialNumber", "inventoryNumber", "badgeNumber"}

def load_lines(path: str, default: list[str]) -> list[str]:
    p = Path(path)
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()] if p.exists() else default

PRODUCE = load_lines(PRODUCE_FILE, ["Томат", "Яблоко", "Груша", "Огурец"])
BASE_NAMES = load_lines(NAMES_FILE, ["Алексей", "Мария", "Дмитрий", "Елена"])

ADJ_EXCEPTIONS = {"Груша": "Грушевый", "Слива": "Сливовый", "Вишня": "Вишнёвый", "Дыня": "Дынный", "Тыква": "Тыквенный", "Огурец": "Огуречный"}

def to_adj(word: str) -> str:
    cap = word.capitalize()
    if cap in ADJ_EXCEPTIONS: return ADJ_EXCEPTIONS[cap]
    wl = word.lower()
    return (wl[:-1] + "ный").capitalize() if wl.endswith(("а", "я")) else (wl + "ный").capitalize()

def seeded_rng(scope: str, value: str, attempt: int = 0) -> random.Random:
    digest = hashlib.sha256(SALT + f"|{scope}|{attempt}|".encode() + value.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))

class Anonymizer:
    def __init__(self, db_path: str = DB_PATH):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS map (scope TEXT, src TEXT, dst TEXT, PRIMARY KEY(scope, src))")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_dst ON map(scope, dst)")
        self.known_text = {}
        self.mem_cache = {}
        self.text_pattern = None

    def get_or_create(self, scope: str, src: str, generator_fn) -> str:
        cache_key = (scope, src)
        if cache_key in self.mem_cache: return self.mem_cache[cache_key]
        row = self.conn.execute("SELECT dst FROM map WHERE scope=? AND src=?", (scope, src)).fetchone()
        if row:
            self.mem_cache[cache_key] = row[0]
            return row[0]
        attempt = 0
        while True:
            cand = generator_fn(src, attempt)
            try:
                self.conn.execute("INSERT INTO map(scope, src, dst) VALUES(?,?,?)", (scope, src, cand))
                self.conn.commit()
                self.mem_cache[cache_key] = cand
                return cand
            except sqlite3.IntegrityError:
                attempt += 1

    def gen_object(self, src: str, attempt: int) -> str:
        rng = seeded_rng("object", src, attempt)
        adj = to_adj(rng.choice(PRODUCE))
        num = rng.randint(100, 999)
        suf = f"-{attempt}" if attempt else ""
        for suffix in (" Regional Dispatching Office", " RDO", " el. net.", " electrical network", " GRID CONTROL CENTER"):
            if src.endswith(suffix): return f"{adj} объект №{num}{suf}{suffix}"
        return f"{adj} объект №{num}{suf}"

    def gen_person(self, src: str, attempt: int) -> str:
        rng = seeded_rng("person", src, attempt)
        base = rng.choice(BASE_NAMES)
        return f"{base}ов{f'-{attempt}' if attempt else ''} {base} {base}ович"

    def gen_digits(self, src: str, attempt: int) -> str:
        if not src: return src
        rng = seeded_rng("digits", src, attempt)
        return "".join(str(rng.randint(0, 9)) for _ in range(len(src)))

    def load_known_text(self):
        for scope, src, dst in self.conn.execute("SELECT scope, src, dst FROM map"):
            if scope in ("object", "person"):
                self.known_text[src] = dst
        
        if self.known_text:
            sorted_keys = sorted(self.known_text.keys(), key=len, reverse=True)
            self.text_pattern = re.compile(r'(?<!\w)(' + '|'.join(map(re.escape, sorted_keys)) + r')(?!\w)')

    def mask_text(self, text: str) -> str:
        if not text or not self.text_pattern: return text
        return self.text_pattern.sub(lambda m: self.known_text[m.group(1)], text)

def process_element(elem, anon: Anonymizer, is_collect: bool):
    for child in elem.iter():
        tag = child.tag
        if not isinstance(tag, str): continue
        localname = tag.rpartition('}')[2]
        
        if localname in NAME_TAGS:
            if child.text and child.text.strip():
                val = child.text.strip()
                scope = "person" if "Person" in localname or "Employee" in localname else "object"
                gen = anon.gen_person if scope == "person" else anon.gen_object
                new_val = anon.get_or_create(scope, val, gen)
                if not is_collect: child.text = new_val
        elif localname in DIGITS_FIELDS:
            if child.text and child.text.strip():
                val = child.text.strip()
                new_val = anon.get_or_create(f"digits:{localname}", val, anon.gen_digits)
                if not is_collect: child.text = new_val
        elif localname in DESC_TAGS:
            if not is_collect and child.text: child.text = anon.mask_text(child.text)

def pass_collect(in_path: str, anon: Anonymizer):
    print("⏳ Проход 1: Сбор данных в кэш (может занять несколько минут)...")
    count = 0
    start_time = time.time()
    
    context = etree.iterparse(in_path, events=("end",), recover=True, huge_tree=True)
    for _, elem in context:
        parent = elem.getparent()
        if parent is None: continue
        
        if parent.getparent() is None:
            process_element(elem, anon, is_collect=True)
            elem.clear()
            while elem.getprevious() is not None:
                del parent[0]
            
            count += 1
            if count % 100000 == 0:
                print(f"   ...собрано {count} верхнеуровневых объектов (прошло {int(time.time()-start_time)} сек)")

    anon.load_known_text()
    print(f"✅ Сбор завершен. Уникальных записей: {len(anon.known_text)}")

def pass_transform(in_path: str, out_path: str, anon: Anonymizer):
    print("⏳ Проход 2: Обезличивание и запись XML...")
    
    context = etree.iterparse(in_path, events=("start",), recover=True, huge_tree=True)
    for _, elem in context:
        root_tag, root_attrib, root_nsmap = elem.tag, dict(elem.attrib), elem.nsmap
        break
    del context

    count = 0
    start_time = time.time()
    
    with etree.xmlfile(out_path, encoding="utf-8", buffered=True) as xf:
        xf.write_declaration()
        xf.write(etree.ProcessingInstruction("iec61970-552", 'version="2.0"'))
        xf.write(etree.ProcessingInstruction("floatExporter", '1'))
        
        with xf.element(root_tag, attrib=root_attrib, nsmap=root_nsmap):
            context = etree.iterparse(in_path, events=("end",), recover=True, huge_tree=True)
            for _, elem in context:
                parent = elem.getparent()
                if parent is None: continue
                
                if parent.getparent() is None:
                    tail = elem.tail
                    elem.tail = None
                    
                    process_element(elem, anon, is_collect=False)
                    xf.write(elem)
                    if tail: xf.write(tail)
                    
                    elem.clear()
                    while elem.getprevious() is not None:
                        del parent[0]
                        
                    count += 1
                    if count % 100000 == 0:
                        print(f"   ...записано {count} объектов (прошло {int(time.time()-start_time)} сек)")
                        
    print(f"✅ Готово! Файл сохранён: {out_path}")

def main():
    if len(sys.argv) != 3:
        print("Использование: python anonymize.py input.xml output.xml")
        sys.exit(1)

    in_path, out_path = sys.argv[1], sys.argv[2]
    anon = Anonymizer()
    
    pass_collect(in_path, anon)
    pass_transform(in_path, out_path, anon)

if __name__ == "__main__":
    main()