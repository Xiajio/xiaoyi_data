"""Closed, versioned CPU recipes: never interpret submitted code."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["1"] = "1"
    operator_version: Literal["cpu/1"] = "cpu/1"
    asset_ids: list[int] = Field(min_length=1, max_length=10000)
    modality: Literal["text", "table", "image"] = "table"
    feature_columns: list[str] = Field(default_factory=list, max_length=256)
    text_column: str | None = None
    label_column: str | None = None
    group_column: str | None = None
    image_path_column: str | None = None
    required_columns: list[str] = Field(default_factory=list, max_length=256)
    allowed_labels: list[str] | None = Field(default=None, max_length=10000)
    labels: dict[str, str] = Field(default_factory=dict, max_length=10000)
    exact_dedup: bool = True
    seed: int = Field(default=42, ge=0, le=2147483647)
    splits: dict[str, float] = Field(default_factory=lambda: {"train": 0.8, "validation": 0.1, "test": 0.1})
    sample_budget: int | None = Field(default=None, gt=0, le=100000000)
    shard_rows: int = Field(default=2000, ge=1, le=10000)
    invalid_policy: Literal["exclude", "fail"] = "exclude"
    @model_validator(mode="after")
    def validate_recipe(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("asset_ids must be unique")
        self.asset_ids = sorted(self.asset_ids)
        if not self.splits or any(k not in {"train", "validation", "test"} or v < 0 or v > 1 for k,v in self.splits.items()) or abs(sum(self.splits.values())-1) > 1e-9:
            raise ValueError("splits must sum to 1 with train/validation/test keys")
        if self.modality == "text" and not self.text_column:
            self.text_column = "text"
        if self.modality != "image" and self.image_path_column:
            raise ValueError("image_path_column requires image modality")
        return self
