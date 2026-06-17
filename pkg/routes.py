from flask import render_template, url_for, request, redirect, flash, session
from pkg import app


@app.route('/')
def home():
    return render_template('index.html', title='Home')


@app.route('/about/')
def about():
    return render_template('about.html', title='About')


@app.route('/contact/')
def contact():
    return render_template('contact.html', title='Contact')


@app.route('/properties/')
def properties():
    return render_template('properties.html', title='Properties')


@app.route('/property-details/')
def property_details():
    return render_template('property-details.html', title='Property Details')


@app.route('/login/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        session['username'] = request.form.get('email', 'guest')
        return redirect(url_for('home'))
    return render_template('login.html', title='Login')


@app.route('/register/', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        session['username'] = request.form.get('email', 'guest')
        return redirect(url_for('home'))
    return render_template('register.html', title='Register')


@app.route('/dashboard/')
def dashboard():
    return render_template('dashboard.html', title='Dashboard')


@app.route('/user/')
def user():
    return render_template('user.html', title='Account Settings')


@app.route('/admin/')
def admin():
    return render_template('admin.html', title='Admin')


@app.route('/testing/')
def testing():
    return render_template('testing.html', title='Testing')


@app.route('/untitled')
def untitled_one():
    return render_template('Untitled-1.html', title='Untitled')


@app.route('/logout/')
def logout():
    session.pop('username', None)
    return redirect(url_for('home'))
