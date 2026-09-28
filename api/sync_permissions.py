from rest_framework import permissions

from content.models import Activity
from elearncore.sysutils.constants import UserRole


class IsScopedSyncService(permissions.BasePermission):
    message = 'A school-scoped sync service account is required.'

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        allowed = bool(
            user
            and user.is_authenticated
            and user.role == UserRole.SYNC_SERVICE.value
            and user.sync_school_id
        )
        if allowed and not getattr(request, '_sync_access_audited', False):
            Activity.objects.create(
                user=user,
                type='sync_api_access',
                description=f'{request.method} {request.path}',
                metadata={
                    'method': request.method,
                    'path': request.path,
                    'school_id': user.sync_school_id,
                },
            )
            request._sync_access_audited = True
        return allowed
