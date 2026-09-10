from typing import List, Optional
from datetime import date,datetime,time
from dotenv import load_dotenv
from urllib.parse import quote_plus
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Numeric, Date, ForeignKey, text,DateTime,Time
from sqlalchemy.orm import declarative_base, sessionmaker, Session


# ============================================================================
# DATABASE CONFIGURATION
# Password encoded via quote_plus for special character '@'
# ============================================================================
DB_PASSWORD = quote_plus("root@123")   # encodes to root%40123
DATABASE_URL = f"postgresql://postgres:{DB_PASSWORD}@127.0.0.1:5432/emp_attendance"

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ============================================================================
# FASTAPI APP & CORS SETUP (INSTANTIATED ONCE)
# ============================================================================
app = FastAPI(
    title="Employee Attendance & Payroll API",
    version="1.0.0",
    description="REST API connecting FastAPI to PostgreSQL database (emp_attendance)",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# SQLALCHEMY ORM MODELS
# ============================================================================
class DepartmentModel(Base):
    __tablename__ = "departments"
    department_id = Column(Integer, primary_key=True, index=True)
    department_name = Column(String, unique=True, nullable=False)


class EmployeeModel(Base):
    __tablename__ = "employees"
    employee_id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    department_id = Column(Integer, ForeignKey("departments.department_id"))
    designation = Column(String, nullable=False)
    joining_date = Column(Date, nullable=False)
    salary = Column(Numeric(10, 2), nullable=False)
    employment_status = Column(String, default="Active")

class AttendanceModel(Base):
    __tablename__ = "attendance"
    attendance_id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.employee_id"))
    attendance_date = Column(Date, nullable=False)
    check_in = Column(Time, nullable=True)
    check_out = Column(Time, nullable=True)
    status = Column(String, nullable=False)
# Automatically create tables in emp_attendance if they don't exist
Base.metadata.create_all(bind=engine)

# ============================================================================
# PYDANTIC SCHEMAS
# ============================================================================
class DepartmentSchema(BaseModel):
    department_id: int
    department_name: str

    class Config:
        from_attributes = True


class EmployeeCreateSchema(BaseModel):
    name: str
    email: str
    department_id: int
    designation: str
    joining_date: date
    salary: float
    employment_status: Optional[str] = "Active"


class EmployeeResponseSchema(EmployeeCreateSchema):
    employee_id: int

    class Config:
        from_attributes = True
        
class AttendanceResponseSchema(BaseModel):
    attendance_id: int
    employee_id: int
    attendance_date: date
    check_in: Optional[time] = None
    check_out: Optional[time] = None
    status: str

    class Config:
        from_attributes = True

# ============================================================================
# ENDPOINTS
# ============================================================================
# --- OVERVIEW STATS ---
@app.get("/analytics/overview", tags=["Analytics"])
def get_overview(db: Session = Depends(get_db)):
    """Quick summary stats for the top of the dashboard."""
    total_employees = db.execute(
        text("SELECT COUNT(*) FROM employees WHERE employment_status = 'Active'")
    ).scalar()

    total_departments = db.execute(text("SELECT COUNT(*) FROM departments")).scalar()

    total_payroll = db.execute(text("""
        SELECT COALESCE(SUM(net_salary), 0) FROM payroll
        WHERE payroll_month = (SELECT MAX(payroll_month) FROM payroll)
    """)).scalar()

    attendance_pct = db.execute(text("""
        SELECT ROUND(
            (COUNT(*) FILTER (WHERE status = 'Present') + COUNT(*) FILTER (WHERE status = 'Half Day') * 0.5)
            / NULLIF(COUNT(*), 0) * 100, 2
        ) FROM attendance
    """)).scalar()

    return {
        "total_employees": total_employees,
        "total_departments": total_departments,
        "total_payroll_latest_month": float(total_payroll) if total_payroll else 0,
        "overall_attendance_pct": float(attendance_pct) if attendance_pct else 0
    }

# ============================================================================
# ENDPOINTS
# ============================================================================
@app.get("/", tags=["Health Check"])
def root():
    return {"message": "Attendance & Payroll System API (emp_attendance) is running successfully!"}


# --- DEPARTMENT ENDPOINTS ---
@app.get("/departments", response_model=List[DepartmentSchema], tags=["Departments"])
def get_departments(db: Session = Depends(get_db)):
    """Fetch all departments."""
    return db.query(DepartmentModel).all()


# --- EMPLOYEE ENDPOINTS ---
@app.get("/employees", response_model=List[EmployeeResponseSchema], tags=["Employees"])
def get_employees(
    skip: int = 0, 
    limit: int = 50, 
    department_id: Optional[int] = None, 
    db: Session = Depends(get_db)
):
    """Fetch paginated list of employees with optional department filtering."""
    query = db.query(EmployeeModel)
    if department_id:
        query = query.filter(EmployeeModel.department_id == department_id)
    return query.offset(skip).limit(limit).all()


@app.get("/employees/{employee_id}", response_model=EmployeeResponseSchema, tags=["Employees"])
def get_employee(employee_id: int, db: Session = Depends(get_db)):
    """Fetch a single employee by ID."""
    employee = db.query(EmployeeModel).filter(EmployeeModel.employee_id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail=f"Employee with ID {employee_id} not found")
    return employee


@app.post("/employees", response_model=EmployeeResponseSchema, status_code=status.HTTP_201_CREATED, tags=["Employees"])
def create_employee(employee: EmployeeCreateSchema, db: Session = Depends(get_db)):
    """Add a new employee to the database."""
    existing = db.query(EmployeeModel).filter(EmployeeModel.email == employee.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    new_employee = EmployeeModel(**employee.model_dump())
    db.add(new_employee)
    db.commit()
    db.refresh(new_employee)
    return new_employee


# --- ATTENDANCE ENDPOINTS ---
@app.get("/attendance/{employee_id}", response_model=List[AttendanceResponseSchema], tags=["Attendance"])
def get_employee_attendance(
    employee_id: int,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """Fetch attendance records for a specific employee, capped at 100 records."""
    limit = min(limit, 100)

    employee = db.query(EmployeeModel).filter(EmployeeModel.employee_id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail=f"Employee with ID {employee_id} not found")

    records = (
        db.query(AttendanceModel)
        .filter(AttendanceModel.employee_id == employee_id)
        .order_by(AttendanceModel.attendance_date.desc())
        .limit(limit)
        .all()
    )
    return records


# --- ANALYTICS DASHBOARD ENDPOINT ---
@app.get("/analytics/hr-summary", tags=["Analytics"])
def get_hr_dashboard_summary(db: Session = Depends(get_db)):
    """Execute raw SQL to query PostgreSQL view (vw_monthly_hr_dashboard)."""
    sql_query = text("SELECT * FROM vw_monthly_hr_dashboard ORDER BY month_year ASC;")
    result = db.execute(sql_query)
    return [dict(row) for row in result.mappings()]


# --- OVERVIEW STATS ---
@app.get("/analytics/overview", tags=["Analytics"])
def get_overview(db: Session = Depends(get_db)):
    """Quick summary stats for the top of the dashboard."""
    total_employees = db.execute(
        text("SELECT COUNT(*) FROM employees WHERE employment_status = 'Active'")
    ).scalar()

    total_departments = db.execute(text("SELECT COUNT(*) FROM departments")).scalar()

    total_payroll = db.execute(text("""
        SELECT COALESCE(SUM(net_salary), 0) FROM payroll
        WHERE payroll_month = (SELECT MAX(payroll_month) FROM payroll)
    """)).scalar()

    attendance_pct = db.execute(text("""
        SELECT ROUND(
            (COUNT(*) FILTER (WHERE status = 'Present') + COUNT(*) FILTER (WHERE status = 'Half Day') * 0.5)
            / NULLIF(COUNT(*), 0) * 100, 2
        ) FROM attendance
    """)).scalar()

    return {
        "total_employees": total_employees,
        "total_departments": total_departments,
        "total_payroll_latest_month": float(total_payroll) if total_payroll else 0,
        "overall_attendance_pct": float(attendance_pct) if attendance_pct else 0
    }


# --- TOP EARNERS ---
@app.get("/analytics/top-earners", tags=["Analytics"])
def get_top_earners(limit: int = 5, db: Session = Depends(get_db)):
    """Highest-paid active employees, capped at 20 even if a larger limit is requested."""
    limit = min(limit, 20)
    sql = text("""
        SELECT e.employee_id, e.name, d.department_name, e.salary
        FROM employees e
        JOIN departments d ON e.department_id = d.department_id
        WHERE e.employment_status = 'Active'
        ORDER BY e.salary DESC
        LIMIT :limit
    """)
    result = db.execute(sql, {"limit": limit})
    return [dict(row) for row in result.mappings()]


# --- LOW ATTENDANCE ALERTS ---
@app.get("/analytics/low-attendance", tags=["Analytics"])
def get_low_attendance(threshold: float = 80.0, db: Session = Depends(get_db)):
    """Employees whose attendance % falls below the given threshold (default 80%)."""
    sql = text("""
        SELECT e.employee_id, e.name, d.department_name,
            ROUND(
                (COUNT(*) FILTER (WHERE a.status = 'Present') + COUNT(*) FILTER (WHERE a.status = 'Half Day') * 0.5)
                / NULLIF(COUNT(*), 0) * 100, 2
            ) AS attendance_pct
        FROM employees e
        JOIN departments d ON e.department_id = d.department_id
        JOIN attendance a ON e.employee_id = a.employee_id
        GROUP BY e.employee_id, e.name, d.department_name
        HAVING ROUND(
            (COUNT(*) FILTER (WHERE a.status = 'Present') + COUNT(*) FILTER (WHERE a.status = 'Half Day') * 0.5)
            / NULLIF(COUNT(*), 0) * 100, 2
        ) < :threshold
        ORDER BY attendance_pct ASC
    """)
    result = db.execute(sql, {"threshold": threshold})
    return [dict(row) for row in result.mappings()]


# --- LEAVE STATISTICS ---
@app.get("/analytics/leave-stats", tags=["Analytics"])
def get_leave_stats(db: Session = Depends(get_db)):
    """Leave request counts grouped by type and status."""
    sql = text("""
        SELECT leave_type, status, COUNT(*) AS total
        FROM leaves
        GROUP BY leave_type, status
        ORDER BY leave_type, status
    """)
    result = db.execute(sql)
    return [dict(row) for row in result.mappings()]


# --- DEPARTMENT SALARY COMPARISON ---
@app.get("/analytics/department-salary", tags=["Analytics"])
def get_department_salary(db: Session = Depends(get_db)):
    """Average salary and headcount per department, for comparison."""
    sql = text("""
        SELECT d.department_name, ROUND(AVG(e.salary), 2) AS avg_salary, COUNT(*) AS employee_count
        FROM departments d
        JOIN employees e ON d.department_id = e.department_id
        WHERE e.employment_status = 'Active'
        GROUP BY d.department_name
        ORDER BY avg_salary DESC
    """)
    result = db.execute(sql)
    return [dict(row) for row in result.mappings()]

# --- TOP EARNERS ---
@app.get("/analytics/top-earners", tags=["Analytics"])
def get_top_earners(limit: int = 5, db: Session = Depends(get_db)):
    """Highest-paid active employees, capped at 20 even if a larger limit is requested."""
    limit = min(limit, 20)
    sql = text("""
        SELECT e.employee_id, e.name, d.department_name, e.salary
        FROM employees e
        JOIN departments d ON e.department_id = d.department_id
        WHERE e.employment_status = 'Active'
        ORDER BY e.salary DESC
        LIMIT :limit
    """)
    result = db.execute(sql, {"limit": limit})
    return [dict(row) for row in result.mappings()]


# --- LOW ATTENDANCE ALERTS ---
@app.get("/analytics/low-attendance", tags=["Analytics"])
def get_low_attendance(threshold: float = 80.0, db: Session = Depends(get_db)):
    """Employees whose attendance % falls below the given threshold (default 80%)."""
    sql = text("""
        SELECT e.employee_id, e.name, d.department_name,
            ROUND(
                (COUNT(*) FILTER (WHERE a.status = 'Present') + COUNT(*) FILTER (WHERE a.status = 'Half Day') * 0.5)
                / NULLIF(COUNT(*), 0) * 100, 2
            ) AS attendance_pct
        FROM employees e
        JOIN departments d ON e.department_id = d.department_id
        JOIN attendance a ON e.employee_id = a.employee_id
        GROUP BY e.employee_id, e.name, d.department_name
        HAVING ROUND(
            (COUNT(*) FILTER (WHERE a.status = 'Present') + COUNT(*) FILTER (WHERE a.status = 'Half Day') * 0.5)
            / NULLIF(COUNT(*), 0) * 100, 2
        ) < :threshold
        ORDER BY attendance_pct ASC
    """)
    result = db.execute(sql, {"threshold": threshold})
    return [dict(row) for row in result.mappings()]


# --- LEAVE STATISTICS ---
@app.get("/analytics/leave-stats", tags=["Analytics"])
def get_leave_stats(db: Session = Depends(get_db)):
    """Leave request counts grouped by type and status."""
    sql = text("""
        SELECT leave_type, status, COUNT(*) AS total
        FROM leaves
        GROUP BY leave_type, status
        ORDER BY leave_type, status
    """)
    result = db.execute(sql)
    return [dict(row) for row in result.mappings()]


# --- DEPARTMENT SALARY COMPARISON ---
@app.get("/analytics/department-salary", tags=["Analytics"])
def get_department_salary(db: Session = Depends(get_db)):
    """Average salary and headcount per department, for comparison."""
    sql = text("""
        SELECT d.department_name, ROUND(AVG(e.salary), 2) AS avg_salary, COUNT(*) AS employee_count
        FROM departments d
        JOIN employees e ON d.department_id = e.department_id
        WHERE e.employment_status = 'Active'
        GROUP BY d.department_name
        ORDER BY avg_salary DESC
    """)
    result = db.execute(sql)
    return [dict(row) for row in result.mappings()]