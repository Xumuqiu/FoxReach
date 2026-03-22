from sqlalchemy.orm import Session

from app.models import Customer


class CustomerService:
    def __init__(self, db: Session):
        self.db = db

    def list_customers(self) -> list[Customer]:
        return self.db.query(Customer).all()
