from django.contrib import admin

from .models import AgentAdapter, AgentProfile, ChangeSet, Task, TaskAssignment, TaskEvent


@admin.register(AgentAdapter)
class AgentAdapterAdmin(admin.ModelAdmin):
    list_display = ("identifier", "display_name", "available", "version", "enabled", "last_probed_at")


@admin.register(AgentProfile)
class AgentProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "adapter", "role", "enabled")


class AssignmentInline(admin.TabularInline):
    model = TaskAssignment
    extra = 0


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "state", "requester", "created_at")
    list_filter = ("state",)
    inlines = [AssignmentInline]


admin.site.register(TaskEvent)
admin.site.register(ChangeSet)
