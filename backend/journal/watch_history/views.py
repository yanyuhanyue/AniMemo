from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from journal.models import JournalEntry, WatchHistoryRecord

from .serializers import (
    WatchHistoryRecordSerializer,
    WatchHistoryReplaceSerializer,
    WatchHistoryWriteSerializer,
)
from .services import add_history, delete_history, list_history, replace_history, update_history
from .validation import WatchHistoryValidationError


def _entry_for(request, entry_id):
    return get_object_or_404(
        JournalEntry,
        pk=entry_id,
        user=request.user,
        deleted_at__isnull=True,
    )


class WatchHistoryCollectionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, entry_id):
        entry = _entry_for(request, entry_id)
        records = list_history(user=request.user, entry=entry)
        return Response({"count": len(records), "results": WatchHistoryRecordSerializer(records, many=True).data})

    def post(self, request, entry_id):
        entry = _entry_for(request, entry_id)
        serializer = WatchHistoryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record, created = add_history(user=request.user, entry=entry, record=serializer.validated_data)
        return Response(
            {"created": created, "record": WatchHistoryRecordSerializer(record).data},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def put(self, request, entry_id):
        entry = _entry_for(request, entry_id)
        serializer = WatchHistoryReplaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            records = replace_history(
                user=request.user,
                entry=entry,
                records=serializer.validated_data["records"],
            )
        except WatchHistoryValidationError as error:
            return Response({"code": error.code, "detail": error.detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"count": len(records), "results": WatchHistoryRecordSerializer(records, many=True).data})


class WatchHistoryDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, entry_id, record_id):
        entry = _entry_for(request, entry_id)
        current = get_object_or_404(WatchHistoryRecord, pk=record_id, entry=entry)
        payload = WatchHistoryRecordSerializer(current).data
        payload.update(request.data)
        serializer = WatchHistoryWriteSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        try:
            updated = update_history(
                user=request.user,
                entry=entry,
                record_id=record_id,
                record=serializer.validated_data,
            )
        except WatchHistoryRecord.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except WatchHistoryValidationError as error:
            return Response({"code": error.code, "detail": error.detail}, status=status.HTTP_409_CONFLICT)
        return Response(WatchHistoryRecordSerializer(updated).data)

    def delete(self, request, entry_id, record_id):
        entry = _entry_for(request, entry_id)
        try:
            delete_history(user=request.user, entry=entry, record_id=record_id)
        except WatchHistoryRecord.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
