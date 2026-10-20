from django.contrib import admin

from .models import ApplicationUser


@admin.register(ApplicationUser)
class ApplicationUserAdmin(admin.ModelAdmin):
    list_display = ('id', 'application', 'email', 'is_active', 'created_at')
    list_filter = ('is_active', 'application')
    search_fields = ('email',)
    readonly_fields = ('password_hash', 'token_version', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        # Use the staff-only API so password hashing and ownership validation apply.
        return False
