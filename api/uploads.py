import csv
import io
from pathlib import Path

from django.conf import settings
from django.utils.module_loading import import_string
from rest_framework import serializers


CSV_CONTENT_TYPES = {'text/csv', 'application/csv', 'application/vnd.ms-excel', 'text/plain'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
IMAGE_CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
DOCUMENT_EXTENSIONS = {'.pdf', '.ppt', '.pptx', '.doc', '.docx', '.mp3', '.mp4', '.wav', '.webm'}
DOCUMENT_CONTENT_TYPES = {
    'application/pdf', 'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'audio/mpeg', 'audio/wav', 'video/mp4', 'video/webm',
}
SOLUTION_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | {'.txt'}
SOLUTION_CONTENT_TYPES = DOCUMENT_CONTENT_TYPES | IMAGE_CONTENT_TYPES | {'text/plain'}


def validate_uploaded_file(upload, *, extensions, content_types, max_bytes, label='file'):
    extension = Path(getattr(upload, 'name', '')).suffix.lower()
    if extension not in extensions:
        raise serializers.ValidationError(f'Unsupported {label} extension.')
    content_type = (getattr(upload, 'content_type', '') or '').lower()
    if content_type and content_type not in content_types:
        raise serializers.ValidationError(f'Unsupported {label} content type.')
    if int(getattr(upload, 'size', 0) or 0) > int(max_bytes):
        raise serializers.ValidationError(f'{label.capitalize()} exceeds the configured size limit.')

    scanner_path = getattr(settings, 'UPLOAD_SCANNER', '')
    if scanner_path:
        scanner = import_string(scanner_path)
        if scanner(upload) is not True:
            raise serializers.ValidationError(f'{label.capitalize()} failed malware scanning.')
        upload.seek(0)
    elif getattr(settings, 'REQUIRE_UPLOAD_SCAN', False):
        raise serializers.ValidationError('Uploads are disabled until malware scanning is configured.')
    return upload


def validate_csv_upload(upload):
    return validate_uploaded_file(
        upload,
        extensions={'.csv'},
        content_types=CSV_CONTENT_TYPES,
        max_bytes=settings.MAX_CSV_UPLOAD_BYTES,
        label='CSV file',
    )


def parse_bounded_csv(upload):
    """Stream-decode a bounded CSV and return its header and validated rows."""
    validate_csv_upload(upload)
    upload.seek(0)
    wrapper = io.TextIOWrapper(upload.file, encoding='utf-8-sig', newline='')
    try:
        reader = csv.DictReader(wrapper)
        rows = []
        for row in reader:
            if len(rows) >= settings.MAX_CSV_ROWS:
                raise serializers.ValidationError(
                    {'file': f'CSV exceeds the maximum of {settings.MAX_CSV_ROWS} data rows.'}
                )
            rows.append(row)
        return reader.fieldnames, rows
    except UnicodeDecodeError as exc:
        raise serializers.ValidationError({'file': 'CSV must be valid UTF-8 text.'}) from exc
    finally:
        wrapper.detach()
        upload.seek(0)


def build_bulk_identity_index(rows):
    """Load existing user identities once for a bounded account-import batch."""
    from django.db.models import Q
    from django.db.models.functions import Lower

    from accounts.models import User

    phones = {(row.get('phone') or '').strip() for row in rows}
    emails = {(row.get('email') or '').strip().lower() for row in rows}
    phones.discard('')
    emails.discard('')
    existing = (
        User.objects
        .annotate(email_lower=Lower('email'))
        .filter(Q(phone__in=phones) | Q(email_lower__in=emails))
        .values('phone', 'email')
    )
    return {
        'existing_phones': {item['phone'] for item in existing if item['phone']},
        'existing_emails': {item['email'].lower() for item in existing if item['email']},
        'seen_phones': set(),
        'seen_emails': set(),
    }


def claim_bulk_identity(index, phone, email):
    """Reserve one identity in the current import and return serializer-style errors."""
    phone = (phone or '').strip()
    email = (email or '').strip().lower()
    errors = {}
    if phone in index['existing_phones']:
        errors['phone'] = ['A user with this phone already exists.']
    elif phone in index['seen_phones']:
        errors['phone'] = ['This phone is duplicated in the CSV.']
    if email:
        if email in index['existing_emails']:
            errors['email'] = ['A user with this email already exists.']
        elif email in index['seen_emails']:
            errors['email'] = ['This email is duplicated in the CSV.']
    if errors:
        return errors
    index['seen_phones'].add(phone)
    if email:
        index['seen_emails'].add(email)
    return None


def build_integer_fk_index(rows, field_name, queryset):
    """Load integer foreign keys referenced by a bounded CSV in one query."""
    object_ids = set()
    for row in rows:
        raw_value = (row.get(field_name) or '').strip()
        if not raw_value:
            continue
        try:
            object_ids.add(int(raw_value))
        except (TypeError, ValueError):
            continue
    return queryset.in_bulk(object_ids)


def validate_image_upload(upload):
    return validate_uploaded_file(
        upload,
        extensions=IMAGE_EXTENSIONS,
        content_types=IMAGE_CONTENT_TYPES,
        max_bytes=settings.MAX_IMAGE_UPLOAD_BYTES,
        label='image',
    )


def validate_document_upload(upload):
    return validate_uploaded_file(
        upload,
        extensions=DOCUMENT_EXTENSIONS,
        content_types=DOCUMENT_CONTENT_TYPES,
        max_bytes=settings.MAX_DOCUMENT_UPLOAD_BYTES,
        label='document',
    )


def validate_solution_upload(upload):
    return validate_uploaded_file(
        upload,
        extensions=SOLUTION_EXTENSIONS,
        content_types=SOLUTION_CONTENT_TYPES,
        max_bytes=settings.MAX_DOCUMENT_UPLOAD_BYTES,
        label='attachment',
    )
