from django.db import models
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone

from django.conf import settings
from elearncore.sysutils.constants import UserRole, StudentLevel, Status as StatusEnum

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
    # onboarding extras
    dob = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, null=True, blank=True)

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
    

# Geography and School structure
class County(TimestampedModel):
    name = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.PENDING.value)
    moderation_comment = models.TextField(blank=True, default="")

    def __str__(self) -> str:
        return self.name


class District(TimestampedModel):
    county = models.ForeignKey(County, on_delete=models.CASCADE, related_name="districts")
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.PENDING.value)
    moderation_comment = models.TextField(blank=True, default="")

    class Meta:
        unique_together = ("county", "name")

    def __str__(self) -> str:
        return f"{self.name} ({self.county.name})"


class School(TimestampedModel):
    district = models.ForeignKey(District, on_delete=models.CASCADE, related_name="schools")
    name = models.CharField(max_length=150)
    status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.PENDING.value)
    moderation_comment = models.TextField(blank=True, default="")

    class Meta:
        unique_together = ("district", "name")

    def __str__(self) -> str:
        return self.name


# Profiles
class Student(TimestampedModel):
    profile = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="student")
    school = models.ForeignKey(School, on_delete=models.SET_NULL, null=True, blank=True, related_name="students")
    grade = models.CharField(max_length=20, choices=[(lvl.value, lvl.value) for lvl in StudentLevel], default=StudentLevel.OTHER.value)
    status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.PENDING.value)
    moderation_comment = models.TextField(blank=True, default="")

    def __str__(self) -> str:
        return f"Student: {self.profile.name}"


class Teacher(TimestampedModel):
    profile = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="teacher")
    school = models.ForeignKey('accounts.School', on_delete=models.SET_NULL, null=True, blank=True, related_name="teachers")
    status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.PENDING.value)
    moderation_comment = models.TextField(blank=True, default="")

    def __str__(self) -> str:
        return f"Teacher: {self.profile.name}"


class Parent(TimestampedModel):
    profile = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="parent")
    wards = models.ManyToManyField(Student, related_name="guardians", blank=True)

    def __str__(self) -> str:
        return f"Parent: {self.profile.name}"


