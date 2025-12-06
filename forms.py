# forms.py

from flask_wtf import FlaskForm
from wtforms import (
    StringField,
    PasswordField,
    BooleanField,
    TextAreaField,
    SubmitField
)
from wtforms.validators import DataRequired, Length, EqualTo


class LoginForm(FlaskForm):
    """
    Used by the /login route to authenticate users.
    """
    username    = StringField(
        'Username',
        validators=[DataRequired(), Length(min=3, max=80)]
    )
    password    = PasswordField(
        'Password',
        validators=[DataRequired()]
    )
    remember_me = BooleanField('Remember Me')
    submit      = SubmitField('Login')


class RegistrationForm(FlaskForm):
    """
    Used by the registration modal on /login to create new users.
    """
    username         = StringField(
        'Username',
        validators=[DataRequired(), Length(min=3, max=80)]
    )
    password         = PasswordField(
        'Password',
        validators=[DataRequired(), Length(min=6)]
    )
    confirm_password = PasswordField(
        'Confirm Password',
        validators=[
            DataRequired(),
            EqualTo('password', message='Passwords must match')
        ]
    )
    submit           = SubmitField('Register')


class EmailForm(FlaskForm):
    """
    Used by the /predict_form route to gather an email’s content.
    """
    email  = TextAreaField(
        'Enter Email Content:',
        validators=[DataRequired()]
    )
    submit = SubmitField('Predict')
