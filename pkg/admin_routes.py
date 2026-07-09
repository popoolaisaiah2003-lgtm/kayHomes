from flask import render_template, request, redirect, url_for, session
from pkg import app


@app.route('/admin/login/', methods=['GET', 'POST'])
def admin_login():

    if request.method == 'POST':
        # Validate admin later
        return redirect(url_for('admin_dashboard'))

    return render_template('admin-login.html', title='Admin Login')


@app.route('/admin/')
def admin_dashboard():
    return render_template('admin.html', title='Admin Dashboard')


@app.route('/admin/logout/')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


