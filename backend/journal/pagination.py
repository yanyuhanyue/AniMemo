from rest_framework.pagination import PageNumberPagination


class FlexiblePageNumberPagination(PageNumberPagination):
    page_size = 24
    page_size_query_param = "page_size"
    max_page_size = 500

    def get_page_size(self, request):
        if request.query_params.get(self.page_size_query_param) == "all":
            return self.max_page_size
        return super().get_page_size(request)

