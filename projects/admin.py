from django.contrib import admin

from .models import AllowlistedRoot, Project


@admin.register(AllowlistedRoot)
class AllowlistedRootAdmin(admin.ModelAdmin):
    list_display = ("alias", "path", "enabled", "created_at")

    def save_model(self, request, obj, form, change):
        from .paths import validate_root_path

        obj.path = str(validate_root_path(obj.path))
        super().save_model(request, obj, form, change)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "root", "relative_path", "git_mode", "owner")
    filter_horizontal = ("members",)
