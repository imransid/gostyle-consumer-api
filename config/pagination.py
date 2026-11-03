"""
The one pagination class every list endpoint uses.

DRF's PageNumberPagination ignores `?page_size=` unless page_size_query_param
is set, which it was not: the app asking for thirty cards quietly received
fifteen and had no way to tell that its parameter had been dropped. Setting it
here rather than on one view keeps /discover and anything added later
answering the same question the same way.
"""

from rest_framework.pagination import PageNumberPagination


class ClientPageNumberPagination(PageNumberPagination):
    """Page size the client may choose, within a cap it may not."""

    page_size_query_param = "page_size"

    # THE CAP IS NOT DECORATION. Every discover row carries roughly ten
    # correlated subqueries — rating, review count, branch, four media
    # lookups, policy, category, story — so the cost of a page is linear in
    # page_size with a large constant. Uncapped, `?page_size=100000` is a
    # database outage available from the address bar, no auth required, on a
    # public endpoint.
    #
    # 50 rather than 100: three screens of cards is already more than any
    # scroll position needs, and the app pages with `next`.
    max_page_size = 50

    # A page size the view has already read and checked itself, when it has
    # one (params._page_size: 422 out of range, never cut to the cap). It
    # wins over ?page_size=. None, the default: ?page_size= as above.
    settled_page_size = None

    def get_page_size(self, request):
        if self.settled_page_size is not None:
            return self.settled_page_size
        return super().get_page_size(request)
