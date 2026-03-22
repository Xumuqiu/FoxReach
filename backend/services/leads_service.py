from sqlalchemy.orm import Session

from app.models import Customer, CustomerAssignment, CustomerState, EmailAccount
from app.repositories.customer_assignment_repository import CustomerAssignmentRepository
from app.repositories.customer_state_repository import CustomerStateRepository


class LeadsService:
    def __init__(self, db: Session):
        self.db = db
        self.state_repository = CustomerStateRepository(db)
        self.assignment_repository = CustomerAssignmentRepository(db)

    def assign_customer(self, customer_id: int, account_id: int) -> CustomerAssignment:
        return self.assignment_repository.upsert(customer_id, account_id)

    def list_replied(self) -> list[dict]:
        rows = (
            self.db.query(Customer, CustomerState, CustomerAssignment, EmailAccount)
            .join(CustomerState, CustomerState.customer_id == Customer.id)
            .outerjoin(CustomerAssignment, CustomerAssignment.customer_id == Customer.id)
            .outerjoin(EmailAccount, EmailAccount.id == CustomerAssignment.account_id)
            .filter(CustomerState.status == "REPLIED")
            .all()
        )

        result: list[dict] = []
        for customer, state, assignment, account in rows:
            result.append(
                {
                    "customer_id": customer.id,
                    "customer_name": customer.name,
                    "customer_email": customer.email,
                    "state": state.status,
                    "sequence_step": state.sequence_step,
                    "assigned_account_id": assignment.account_id if assignment else None,
                    "assigned_salesperson": account.salesperson_name if account else None,
                    "assigned_email": account.email_address if account else None,
                }
            )
        return result
