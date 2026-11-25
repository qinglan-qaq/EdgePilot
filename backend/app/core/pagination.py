"""Pagination primitives shared by every list endpoint.

This module is the single implementation of the ``?page=&size=`` contract;
``app.schemas.common`` re-exports ``PageParams`` / ``PageResult`` so callers can
keep importing them from either place.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int
