from django.db import models
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone

from elearncore.sysutils.constants import UserRole

from .manager import AccountManager


class TimestampedModel(models.Model):
    '''An abstract base class model that provides self-updating 
    'created' and 'modified' fields to any model that inherits from it.'''
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

class User(AbstractBaseUser, PermissionsMixin):
    '''Custom User model for the application'''
    email = models.EmailField(max_length=50, unique=True, null=True, blank=True)
    phone = models.CharField(max_length=25, unique=True) #we sometimes pass the email as phone
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=50, default=UserRole.STUDENT.value)
    deleted = models.BooleanField(default=False)  # Soft delete
    
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AccountManager()

    USERNAME_FIELD = 'phone'
    REQUIRED_FIELDS = ['name', 'email']


    def __str__(self):
        return self.name

class OTP(TimestampedModel):
    '''One Time Password model'''
    phone = models.CharField(max_length=12)
    otp = models.CharField(max_length=6)

    def is_expired(self) -> bool:
        '''Returns True if the OTP is expired'''
        return (self.created_at + timedelta(minutes=30)) < timezone.now()
    
    def send_otp(self) -> None:
        '''Send the OTP to the user'''
        from messsaging.services import send_sms
        message = f"Welcome to the Liberia eLearn platform.\n\nYour OTP is {self.otp}.\n\nPlease do not share this with anyone."
        send_sms(message, [self.phone])


    def __str__(self):
        return self.phone + ' - ' + str(self.otp)