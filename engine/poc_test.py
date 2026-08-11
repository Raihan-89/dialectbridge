import sqlglot

# A realistic T-SQL CREATE TABLE statement with SQL Server-specific constructs
tsql_statement = """
CREATE TABLE Employees (
    EmployeeID INT IDENTITY(1,1) PRIMARY KEY,
    FirstName NVARCHAR(50) NOT NULL,
    LastName NVARCHAR(50) NOT NULL,
    HireDate DATETIME DEFAULT GETDATE(),
    Salary MONEY,
    IsActive BIT DEFAULT 1
)
"""

# Convert T-SQL -> PostgreSQL
postgres_result = sqlglot.transpile(
    tsql_statement,
    read="tsql",
    write="postgres",
    pretty=True
)

print("=== ORIGINAL T-SQL ===")
print(tsql_statement)

print("\n=== CONVERTED TO POSTGRESQL ===")
for statement in postgres_result:
    print(statement)