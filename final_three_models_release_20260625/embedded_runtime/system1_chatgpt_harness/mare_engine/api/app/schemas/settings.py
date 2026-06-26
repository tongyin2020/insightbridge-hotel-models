from pydantic import BaseModel, field_validator, model_validator


class HotelSettingsUpdate(BaseModel):
    floor_price: float
    ceiling_price: float
    base_price: float

    @field_validator("floor_price", "ceiling_price", "base_price")
    @classmethod
    def validate_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Price must be positive")
        if v > 100000:
            raise ValueError("Price unreasonably high")
        return v

    @model_validator(mode="after")
    def validate_price_order(self):
        if self.floor_price >= self.ceiling_price:
            raise ValueError("floor_price must be less than ceiling_price")
        if self.base_price < self.floor_price or self.base_price > self.ceiling_price:
            raise ValueError("base_price must be between floor_price and ceiling_price")
        return self
