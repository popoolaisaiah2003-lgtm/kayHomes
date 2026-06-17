import pathlib
import re

base = pathlib.Path(__file__).parent / 'templates'
old_prefix = "{{ url_for(\\'static\\', filename=\\'"
new_prefix = "{{ url_for('static', filename='"
old_suffix = "\\') }}"
new_suffix = "') }}"

for path in sorted(base.glob('*.html')):
    text = path.read_text(encoding='utf-8')
    new = text.replace(old_prefix, new_prefix).replace(old_suffix, new_suffix)
    if new != text:
        path.write_text(new, encoding='utf-8')
        print(f'Fixed {path.name}')
