# app/crud.py

from sqlalchemy.orm import Session,joinedload
from . import models, schemas
from .security import get_password_hash
from datetime import datetime, timedelta
from .models import UserRole, SalesCaseStatus 
from typing import Optional
# --- CRUD para Users ---

def get_user_by_email(db: Session, email: str):
    return db.query(models.User).filter(models.User.email == email).first()

def create_user(db: Session, user: schemas.UserCreate):
    hashed_password = get_password_hash(user.password)
    db_user = models.User(
        email=user.email,
        hashed_password=hashed_password,
        is_admin=user.is_admin
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

# --- CRUD para Products ---

def get_product(db: Session, product_id: int):
    return db.query(models.Product).filter(models.Product.id == product_id).first()

def get_product_by_barcode(db: Session, barcode: str):
    if not barcode:
        return None
    return db.query(models.Product).filter(models.Product.barcode == barcode).first()

def get_products(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.Product).offset(skip).limit(limit).all()

def create_product(db: Session, product: schemas.ProductCreate):
    db_product = models.Product(**product.model_dump())
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return db_product

def update_product(db: Session, db_product: models.Product, product_update: schemas.ProductUpdate):
    update_data = product_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_product, key, value)
    
    db.add(db_product) # ou db.commit() diretamente se não houver mais nada na sessão
    db.commit()
    db.refresh(db_product)
    return db_product

def delete_product(db: Session, db_product: models.Product):
    db.delete(db_product)
    db.commit()

def create_order(db: Session, user: models.User, order_create: schemas.OrderCreate):
    """
    Cria uma nova encomenda, incluindo os seus itens e atualizando o stock.
    Esta função é transacional. Ou tudo funciona, ou nada é salvo.
    """
    try:
        # 1. Criar o registo principal da Encomenda (Order)
        db_order = models.Order(
            user_id=user.id,
            status="processing" # Um status inicial
        )
        db.add(db_order)
        # Usamos flush para obter o ID da encomenda antes do commit final
        db.flush()

        total_order_price = 0 # (Opcional, mas útil)

        # 2. Iterar sobre os itens do carrinho
        for item in order_create.items:
            # Obter o produto da base de dados para verificar stock e preço
            db_product = get_product(db, product_id=item.product_id)

            # 2a. Validações de negócio CRÍTICAS
            if not db_product:
                raise ValueError(f"Product with id {item.product_id} not found.")
            if db_product.stock_quantity < item.quantity:
                raise ValueError(f"Insufficient stock for product '{db_product.name}'. Available: {db_product.stock_quantity}, Requested: {item.quantity}")

            # 2b. Criar o Item da Encomenda (OrderItem)
            db_order_item = models.OrderItem(
                order_id=db_order.id,
                product_id=item.product_id,
                quantity=item.quantity,
                price_at_purchase=db_product.price # "Congelamos" o preço aqui!
            )
            db.add(db_order_item)

            # 2c. Atualizar o stock do produto
            db_product.stock_quantity -= item.quantity

        # 3. Se tudo correu bem até aqui, confirmar a transação
        db.commit()
        # Refresh para carregar as relações (como a lista de 'items')
        db.refresh(db_order)
        return db_order

    except ValueError as e:
        # 4. Se qualquer validação falhou, reverter TODAS as alterações
        db.rollback()
        # Re-levantar a exceção para que o endpoint possa tratá-la
        raise e


def get_orders_by_user(db: Session, user_id: int, skip: int = 0, limit: int = 25):
    """
    Busca uma lista paginada de encomendas para um utilizador específico.
    Usa 'joinedload' para carregar os itens da encomenda de forma eficiente
    e evitar o problema N+1.
    """
    return (
        db.query(models.Order)
        .filter(models.Order.user_id == user_id) # Filtro de segurança crucial
        .order_by(models.Order.id.desc())         # Encomendas mais recentes primeiro
        .options(joinedload(models.Order.items).joinedload(models.OrderItem.product)) # Carregamento eficiente
        .offset(skip)
        .limit(limit)
        .all()
    )

def get_product_by_barcode(db: Session, barcode: str):
    """
    Busca um único produto pelo seu código de barras.
    Aproveita o índice da base de dados para uma busca rápida.
    """
    if not barcode:
        return None
    return db.query(models.Product).filter(models.Product.barcode == barcode).first()

def create_sales_case(db: Session, case_create: schemas.SalesCaseCreate):
    """
    Cria um novo estojo de vendas. Operação transacional e segura.
    1. Valida a vendedora.
    2. Valida a disponibilidade de stock para cada produto.
    3. Cria o estojo e os seus itens.
    4. Atualiza o stock 'on_loan' de cada produto.
    Tudo dentro de uma transação: ou tudo funciona, ou nada é salvo.
    """
    # Usamos um bloco try/except para garantir a atomicidade com db.rollback()
    try:
        # 1. Validar a vendedora (Sales Rep)
        sales_rep = db.query(models.User).filter(
            models.User.id == case_create.sales_rep_id,
            models.User.role == UserRole.SALES_REP
        ).first()

        if not sales_rep:
            raise ValueError(f"Sales representative with id {case_create.sales_rep_id} not found or is not a sales_rep.")

        # 2. Validar o stock de TODOS os produtos ANTES de qualquer alteração
        for item in case_create.items:
            product = db.query(models.Product).filter(models.Product.id == item.product_id).first()
            if not product:
                raise ValueError(f"Product with id {item.product_id} not found.")
            
            available_stock = product.stock_quantity - product.on_loan_quantity
            if available_stock < item.quantity:
                raise ValueError(f"Insufficient available stock for product '{product.name}' (ID: {product.id}). Available: {available_stock}, Requested: {item.quantity}")

        # 3. Criar o registo principal do Estojo (SalesCase)
        return_by_date = datetime.utcnow() + timedelta(days=case_create.loan_duration_days)
        db_case = models.SalesCase(
            sales_rep_id=case_create.sales_rep_id,
            return_by_date=return_by_date
        )
        db.add(db_case)
        db.flush() # Para obter o db_case.id para os itens

        # 4. Criar os Itens do Estojo e ATUALIZAR o stock 'on_loan'
        for item in case_create.items:
            # Re-buscar o produto para garantir que estamos a trabalhar com o objeto da sessão
            product_to_update = db.query(models.Product).filter(models.Product.id == item.product_id).first()
            
            # Criar o item do estojo
            db_case_item = models.SalesCaseItem(
                case_id=db_case.id,
                product_id=item.product_id,
                quantity=item.quantity
            )
            db.add(db_case_item)
            
            # Atualizar o inventário
            product_to_update.on_loan_quantity += item.quantity

        # 5. Se tudo correu bem, confirmar a transação
        db.commit()
        db.refresh(db_case)
        return db_case

    except ValueError as e:
        # 6. Se qualquer validação falhou, reverter TODAS as alterações
        db.rollback()
        # Re-levantar a exceção para que o endpoint a possa tratar
        raise e
    
def get_sales_case(db: Session, case_id: int):
    """
    Busca um único estojo pelo seu ID, carregando de forma otimizada
    os itens e os detalhes dos produtos associados.
    """
    return (
        db.query(models.SalesCase)
        .filter(models.SalesCase.id == case_id)
        .options(
            joinedload(models.SalesCase.items)
            .joinedload(models.SalesCaseItem.product)
        )
        .first()
    )

def get_sales_cases(
    db: Session, 
    current_user: models.User, 
    status: Optional[SalesCaseStatus] = None, 
    sales_rep_id: Optional[int] = None
):
    """
    Busca uma lista de estojos com base em filtros e permissões.
    - ADMINS podem filtrar por qualquer vendedora.
    - SALES_REPs veem apenas os seus próprios estojos.
    """
    query = db.query(models.SalesCase).order_by(models.SalesCase.id.desc())

    # --- Lógica de Segurança e Filtragem ---
    if current_user.role == UserRole.SALES_REP:
        # Força o filtro para a própria vendedora, ignorando o query param
        query = query.filter(models.SalesCase.sales_rep_id == current_user.id)
    elif current_user.role == UserRole.ADMIN and sales_rep_id:
        # Admin pode filtrar por uma vendedora específica
        query = query.filter(models.SalesCase.sales_rep_id == sales_rep_id)

    if status:
        query = query.filter(models.SalesCase.status == status)
    
    return query.all()

def process_sales_case_return(db: Session, case_id: int, return_request: schemas.SalesCaseReturnRequest, current_user: models.User):
    """
    Processa a devolução de um estojo. Operação transacional de missão crítica.
    """
    try:
        # 1. Validação inicial do estojo
        db_case = get_sales_case(db, case_id) # Reutilizamos a nossa função otimizada
        if not db_case:
            raise ValueError("Sales case not found.")
        if db_case.status != SalesCaseStatus.ON_LOAN:
            raise ValueError(f"Sales case is not in '{SalesCaseStatus.ON_LOAN.value}' status.")
            
        # 2. Validação de autorização
        if current_user.role == UserRole.SALES_REP and db_case.sales_rep_id != current_user.id:
            raise PermissionError("Not authorized to return this sales case.")

        # 3. Preparar dados para processamento
        loaned_items_map = {item.product_id: item.quantity for item in db_case.items}
        items_sold_map = {item.product_id: item.quantity_sold for item in return_request.items_sold}
        
        items_summary_report = []
        total_items_sold = 0
        total_value_sold = 0.0

        # 4. Processar cada produto que estava no estojo
        for product_id, quantity_loaned in loaned_items_map.items():
            quantity_sold = items_sold_map.get(product_id, 0)

            if quantity_sold > quantity_loaned:
                raise ValueError(f"Cannot sell more items than were loaned for product ID {product_id}.")

            quantity_returned = quantity_loaned - quantity_sold
            
            # Atualizar o inventário do produto
            product = db.query(models.Product).filter(models.Product.id == product_id).first()
            if not product: # Sanity check
                raise ValueError(f"Product with ID {product_id} from case seems to be missing.")
            
            product.on_loan_quantity -= quantity_loaned
            # VIBECODER CORRECTION: A instrução original era "aumentar stock pelo retornado".
            # A lógica correta é diminuir o stock físico total pelo que foi vendido.
            product.stock_quantity -= quantity_sold 

            # Construir o relatório de resumo
            subtotal = quantity_sold * float(product.price)
            items_summary_report.append(schemas.ItemReturnSummary(
                product_name=product.name,
                quantity_loaned=quantity_loaned,
                quantity_sold=quantity_sold,
                quantity_returned=quantity_returned,
                price_per_item=float(product.price),
                subtotal_sold=subtotal,
            ))
            total_items_sold += quantity_sold
            total_value_sold += subtotal

        # 5. Gerar o registo de venda (Order), se algo foi vendido
        new_order_id = None
        if total_items_sold > 0:
            new_order = models.Order(user_id=db_case.sales_rep_id, status="completed_by_sales_rep")
            db.add(new_order)
            db.flush() # Para obter o new_order.id

            for item in return_request.items_sold:
                if item.quantity_sold > 0:
                    product = db.query(models.Product).filter(models.Product.id == item.product_id).first()
                    order_item = models.OrderItem(
                        order_id=new_order.id,
                        product_id=item.product_id,
                        quantity=item.quantity_sold,
                        price_at_purchase=product.price
                    )
                    db.add(order_item)
            new_order_id = new_order.id

        # 6. Finalizar o estojo
        db_case.status = SalesCaseStatus.RETURNED
        
        # 7. Confirmar a transação
        db.commit()

        # 8. Construir e retornar o relatório final
        return schemas.SalesCaseReturnReport(
            case_id=case_id,
            new_order_id=new_order_id,
            sales_rep_id=db_case.sales_rep_id,
            date_returned=datetime.utcnow(),
            total_items_sold=total_items_sold,
            total_value_sold=total_value_sold,
            items_summary=items_summary_report
        )

    except (ValueError, PermissionError) as e:
        db.rollback()
        raise e