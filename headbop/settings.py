"""Validated rendering options, with explicit preservation of the original engine."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["whole", "subject"] = "whole"
    motion: Literal["smooth", "hard", "velocity", "auto"] = "auto"
    bpm: float = Field(ge=40, le=240)
    intensity: float = Field(default=1, ge=0, le=2)
    engine: Literal["legacy", "v2"] = "v2"
    fps: Literal[24, 30, 60] = 24
    resolution: Literal[640, 720, 1080] = 640
    aspect: Literal["source", "square", "vertical"] = "source"
    speed: float = Field(default=1, ge=0.5, le=2)
    easing: Literal["natural", "linear", "ease-in-out"] = "natural"
    squash: float = Field(default=0.25, ge=0, le=1)

    @field_validator("bpm", "intensity", "speed", "squash", mode="before")
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("Informe um número, não um booleano.")
        return value

    @model_validator(mode="after")
    def legacy_contract(self):
        if self.engine == "legacy":
            if self.motion == "auto":
                raise ValueError("O motor original usa smooth, hard ou velocity.")
            if (self.fps, self.resolution, self.aspect, self.speed, self.easing, self.squash) != (
                    24, 640, "source", 1, "natural", 0.25):
                raise ValueError("Use o motor 2.0 para alterar saída, easing ou deformação.")
        return self


def effective_settings(value: dict) -> dict:
    """Missing engine denotes an existing v1 project, never a silent migration."""
    return Settings.model_validate({"engine": "legacy", **value}).model_dump()
