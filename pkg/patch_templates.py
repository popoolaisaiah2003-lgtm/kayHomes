import pathlib
import re

base = pathlib.Path(__file__).parent / 'templates'
route_map = {
    'index.html': "{{ url_for('home') }}",
    'about.html': "{{ url_for('about') }}",
    'contact.html': "{{ url_for('contact') }}",
    'properties.html': "{{ url_for('properties') }}",
    'property-details.html': "{{ url_for('property_details') }}",
    'dashboard.html': "{{ url_for('dashboard') }}",
    'user.html': "{{ url_for('user') }}",
    'login.html': "{{ url_for('login') }}",
    'register.html': "{{ url_for('register') }}",
    'admin.html': "{{ url_for('admin') }}",
    'testing.html': "{{ url_for('testing') }}",
    'Untitled-1.html': "{{ url_for('untitled_one') }}",
    'add-property.html': "{{ url_for('property_details') }}",
}
static_map = {
    'bootstrap/css/bootstrap.min.css': "{{ url_for('static', filename='bootstrap/css/bootstrap.min.css') }}",
    'homes.css': "{{ url_for('static', filename='homes.css') }}",
    'bootstrap/js/bootstrap.bundle.min.js': "{{ url_for('static', filename='bootstrap/js/bootstrap.bundle.min.js') }}",
}

for path in sorted(base.glob('*.html')):
    text = path.read_text(encoding='utf-8')
    original = text

    for old, new in route_map.items():
        text = text.replace(f'href="{old}"', f'href="{new}"')
        text = text.replace(f"href='{old}'", f"href='{new}'")
        text = text.replace(f'src="{old}"', f'src="{new}"')
        text = text.replace(f"src='{old}'", f"src='{new}'")

    for old, new in static_map.items():
        text = text.replace(f'href="{old}"', f'href="{new}"')
        text = text.replace(f"href='{old}'", f"href='{new}'")
        text = text.replace(f'src="{old}"', f'src="{new}"')
        text = text.replace(f"src='{old}'", f"src='{new}'")

    text = re.sub(r'src="/static/([^"]+)"', r'src="{{ url_for(\'static\', filename=\'\1\') }}"', text)
    text = re.sub(r'href="/static/([^"]+)"', r'href="{{ url_for(\'static\', filename=\'\1\') }}"', text)
    text = re.sub(r'src="images/([^"]+)"', r'src="{{ url_for(\'static\', filename=\'images/\1\') }}"', text)
    text = re.sub(r"src='images/([^']+)'", r"src='{{ url_for(\'static\', filename=\'images/\1\') }}'", text)
    text = re.sub(r'href="images/([^"]+)"', r'href="{{ url_for(\'static\', filename=\'images/\1\') }}"', text)
    text = re.sub(r"href='images/([^']+)'", r"href='{{ url_for(\'static\', filename=\'images/\1\') }}'", text)

    if text != original:
        path.write_text(text, encoding='utf-8')
        print(f'Updated {path.name}')
