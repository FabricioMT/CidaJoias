# app/routers/sales_cases.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from .. import models, schemas, auth, crud
from ..database import get_db
from ..models import SalesCaseStatus,UserRole

router = APIRouter(
    prefix="/sales-cases",
    tags=["Sales Cases"]
)

@router.post("/", response_model=schemas.SalesCaseResponse, status_code=status.HTTP_201_CREATED)
def create_new_sales_case(
    case_create: schemas.SalesCaseCreate,
    db: Session = Depends(get_db),
    # Apenas administradores podem criar e atribuir estojos
    current_admin: models.User = Depends(auth.require_admin_user)
):
    """
    Cria um novo estojo de vendas para uma vendedora.
    - Requer privilégios de Administrador.
    - Valida o stock disponível.
    - Operação transacional.
    """
    try:
        new_case = crud.create_sales_case(db=db, case_create=case_create)
        return new_case
    except ValueError as e:
        # Captura os erros de lógica de negócio do CRUD e os converte em erros HTTP
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    
@router.get("/", response_model=List[schemas.SalesCaseResponse])
def read_sales_cases(
    status: Optional[SalesCaseStatus] = None,
    sales_rep_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_admin_or_sales_rep)
):
    """
    Lista os estojos de vendas.
    - ADMINS: Podem ver todos e filtrar por 'sales_rep_id'.
    - SALES_REPS: Veem apenas os seus próprios estojos.
    """
    cases = crud.get_sales_cases(db, current_user, status=status, sales_rep_id=sales_rep_id)
    return cases

@router.get("/{case_id}", response_model=schemas.SalesCaseResponse)
def read_sales_case(
    case_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_admin_or_sales_rep)
):
    """
    Obtém os detalhes de um estojo de vendas específico.
    """
    db_case = crud.get_sales_case(db, case_id=case_id)
    
    if db_case is None:
        raise HTTPException(status_code=404, detail="Sales case not found")
        
    # --- Lógica de Autorização ---
    if current_user.role == UserRole.SALES_REP and db_case.sales_rep_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this sales case")
        
    return db_case

@router.post("/{case_id}/return", response_model=schemas.SalesCaseReturnReport)
def return_sales_case(
    case_id: int,
    return_request: schemas.SalesCaseReturnRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_admin_or_sales_rep)
):
    """
    Finaliza um estojo, processa as vendas e devolve o stock restante.
    Esta é uma operação transacional de alta importância.
    """
    try:
        report = crud.process_sales_case_return(db, case_id, return_request, current_user)
        return report
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))