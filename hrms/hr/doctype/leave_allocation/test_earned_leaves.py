import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import (
	add_days,
	add_months,
	date_diff,
	flt,
	get_first_day,
	get_last_day,
	get_year_ending,
	get_year_start,
	getdate,
)

from erpnext.setup.doctype.holiday_list.test_holiday_list import set_holiday_list

from hrms.hr.doctype.leave_allocation.test_leave_allocation import create_leave_allocation
from hrms.hr.doctype.leave_application.leave_application import (
	get_leave_balance_on,
	get_leave_details,
)
from hrms.hr.doctype.leave_application.test_leave_application import make_leave_application
from hrms.hr.doctype.leave_policy_assignment.leave_policy_assignment import (
	calculate_pro_rated_leaves,
	create_assignment_for_multiple_employees,
)
from hrms.hr.utils import allocate_earned_leaves
from hrms.payroll.doctype.salary_slip.test_salary_slip import make_holiday_list
from hrms.tests.test_utils import get_first_sunday


class TestLeaveAllocation(FrappeTestCase):
	def setUp(self):
		for doctype in [
			"Leave Period",
			"Leave Application",
			"Leave Allocation",
			"Leave Policy Assignment",
			"Leave Ledger Entry",
		]:
			frappe.db.delete(doctype)

		employee = frappe.get_doc("Employee", "_T-Employee-00001")
		self.original_doj = employee.date_of_joining

		employee.date_of_joining = add_months(getdate(), -24)
		employee.save()

		self.employee = employee
		self.leave_type = "Test Earned Leave"

		from_date = get_year_start(getdate())
		to_date = get_year_ending(getdate())
		self.holiday_list = make_holiday_list(from_date=from_date, to_date=to_date)

	def test_earned_leave_allocation(self):
		"""Tests if Earned Leave allocation is 0 initially as it happens via scheduler"""
		# second last day of the month
		# leaves allocated should be 0 since it is an earned leave and allocation happens via scheduler based on set frequency
		frappe.flags.current_date = add_days(get_last_day(getdate()), -1)
		leave_policy_assignments = make_policy_assignment(self.employee)

		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 0)

	def test_alloc_based_on_leave_period(self):
		"""Case 1: Tests if assignment created one month after the leave period
		allocates 1 leave for past month"""
		start_date = get_first_day(add_months(getdate(), -1))

		frappe.flags.current_date = get_first_day(getdate())
		leave_policy_assignments = make_policy_assignment(self.employee, start_date=start_date)

		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 1)

	def test_alloc_on_month_end_based_on_leave_period(self):
		"""Case 2: Tests if assignment created on the last day of the leave period's latter month
		allocates 1 leave for the current month even though the month has not ended
		since the daily job might have already executed (12:00:00 AM)"""
		start_date = get_first_day(add_months(getdate(), -2))

		frappe.flags.current_date = get_last_day(getdate())
		leave_policy_assignments = make_policy_assignment(self.employee, start_date=start_date)

		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 3)

	def test_alloc_based_on_leave_period_with_cf_leaves(self):
		"""Case 3: Tests assignment created on the leave period's latter month with carry forwarding"""
		start_date = get_first_day(add_months(getdate(), -2))

		# initial leave allocation = 5
		leave_allocation = create_leave_allocation(
			employee=self.employee.name,
			employee_name=self.employee.employee_name,
			leave_type="Test Earned Leave",
			from_date=add_months(getdate(), -12),
			to_date=add_months(getdate(), -3),
			new_leaves_allocated=5,
			carry_forward=0,
		)
		leave_allocation.submit()

		frappe.flags.current_date = get_last_day(add_months(getdate(), -1))
		# carry forwarded leaves = 5, 2 leaves allocated for passed months
		leave_policy_assignments = make_policy_assignment(
			self.employee, start_date=start_date, carry_forward=1
		)

		details = frappe.db.get_value(
			"Leave Allocation",
			{"leave_policy_assignment": leave_policy_assignments[0]},
			["total_leaves_allocated", "new_leaves_allocated", "unused_leaves", "name"],
			as_dict=True,
		)
		self.assertEqual(details.new_leaves_allocated, 2)
		self.assertEqual(details.unused_leaves, 5)
		self.assertEqual(details.total_leaves_allocated, 7)

	def test_alloc_based_on_joining_date(self):
		"""Tests if DOJ-based assignment created 2 months after the DOJ
		allocates 3 leaves for the past 2 months"""
		self.employee.date_of_joining = get_first_day(add_months(getdate(), -2))
		self.employee.save()

		# assignment created on the last day of the current month
		frappe.flags.current_date = get_last_day(getdate())

		leave_policy_assignments = make_policy_assignment(
			self.employee, assignment_based_on="Joining Date"
		)
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		effective_from = frappe.db.get_value(
			"Leave Policy Assignment", leave_policy_assignments[0], "effective_from"
		)
		self.assertEqual(effective_from, self.employee.date_of_joining)
		self.assertEqual(leaves_allocated, 3)

	def test_alloc_on_doj_based_on_leave_period(self):
		"""Tests assignment with 'Allocate On=Date of Joining' based on Leave Period"""
		start_date = get_first_day(add_months(getdate(), -2))

		# joining date set to 2 months back
		self.employee.date_of_joining = start_date
		self.employee.save()

		# assignment created on the same day of the current month, should allocate leaves including the current month
		frappe.flags.current_date = get_first_day(getdate())

		leave_policy_assignments = make_policy_assignment(
			self.employee, start_date=start_date, allocate_on_day="Date of Joining"
		)
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 3)

	def test_alloc_on_doj_based_on_joining_date(self):
		"""Tests assignment with 'Allocate On=Date of Joining' based on Joining Date"""
		# joining date set to 2 months back
		# leave should be allocated for current month too since this day is same as the joining day
		self.employee.date_of_joining = get_first_day(add_months(getdate(), -2))
		self.employee.save()

		# assignment created on the first day of the current month
		frappe.flags.current_date = get_first_day(getdate())

		leave_policy_assignments = make_policy_assignment(
			self.employee, allocate_on_day="Date of Joining", assignment_based_on="Joining Date"
		)
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		effective_from = frappe.db.get_value(
			"Leave Policy Assignment", leave_policy_assignments[0], "effective_from"
		)
		self.assertEqual(effective_from, self.employee.date_of_joining)
		self.assertEqual(leaves_allocated, 3)

	def test_earned_leaves_creation(self):
		test = get_year_start(getdate())
		frappe.flags.current_date = get_year_start(getdate())
		make_policy_assignment(
			self.employee,
			annual_allocation=6,
			allocate_on_day="First Day",
			start_date=frappe.flags.current_date,
		)

		# leaves for 6 months = 3, but max leaves restricts allocation to 2
		frappe.db.set_value("Leave Type", self.leave_type, "max_leaves_allowed", 2)
		allocate_earned_leaves_for_months(6)
		self.assertEqual(
			get_leave_balance_on(self.employee.name, self.leave_type, frappe.flags.current_date), 2
		)

		# validate earned leaves creation without maximum leaves
		frappe.db.set_value("Leave Type", self.leave_type, "max_leaves_allowed", 0)
		allocate_earned_leaves_for_months(6)
		self.assertEqual(
			get_leave_balance_on(self.employee.name, self.leave_type, frappe.flags.current_date), 5
		)

	def test_allocate_on_first_day(self):
		"""Tests assignment with 'Allocate On=First Day'"""
		start_date = get_first_day(add_months(getdate(), -1))
		prev_month_last_day = get_last_day(add_months(getdate(), -1))
		first_day = get_first_day(getdate())

		# Case 1: Allocates 1 leave for the previous month if created on the previous month's last day
		frappe.flags.current_date = prev_month_last_day
		leave_policy_assignments = make_policy_assignment(
			self.employee, allocate_on_day="First Day", start_date=start_date
		)
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 1)

		# Case 2: Allocates 1 leave on the current month's first day (via scheduler)
		frappe.flags.current_date = first_day
		allocate_earned_leaves()
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 2)

	def test_allocate_on_last_day(self):
		"""Tests assignment with 'Allocate On=Last Day'"""
		prev_month_last_day = get_last_day(add_months(getdate(), -1))
		last_day = get_last_day(getdate())

		# Case 1: Allocates 1 leave for the previous month if created on the previous month's last day
		frappe.flags.current_date = prev_month_last_day
		leave_policy_assignments = make_policy_assignment(
			self.employee, allocate_on_day="Last Day", start_date=prev_month_last_day
		)
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 1)

		# Case 2: Allocates 1 leave on the current month's last day (via scheduler)
		frappe.flags.current_date = last_day
		allocate_earned_leaves()
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 2)

		# Case 3: Doesn't allocate before the current month's last day (via scheduler)
		frappe.flags.current_date = add_days(last_day, -1)
		allocate_earned_leaves()
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		# balance is still 2
		self.assertEqual(leaves_allocated, 2)

	def test_allocate_on_date_of_joining(self):
		"""Tests assignment with 'Allocate On=Date of Joining'"""
		start_date = get_first_day(add_months(getdate(), -1))
		end_date = get_last_day(start_date)
		doj = add_days(start_date, 5)
		current_month_doj = add_days(get_first_day(getdate()), 5)

		self.employee.date_of_joining = doj
		self.employee.save()

		# Case 1: Allocates pro-rated leave for the previous month if created on the previous month's day of joining
		frappe.flags.current_date = doj
		leave_policy_assignments = make_policy_assignment(
			self.employee, allocate_on_day="Date of Joining", start_date=start_date
		)
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		pro_rated_leave = calculate_pro_rated_leaves(1, doj, start_date, end_date)
		self.assertEqual(leaves_allocated, pro_rated_leave)

		# Case 2: Doesn't allocate before the current month's doj (via scheduler)
		frappe.flags.current_date = add_days(current_month_doj, -1)
		allocate_earned_leaves()
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		# balance is still the same
		self.assertEqual(leaves_allocated, pro_rated_leave)

		# Case 3: Allocates 1 leave on the current month's day of joining (via scheduler)
		frappe.flags.current_date = current_month_doj
		allocate_earned_leaves()
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, pro_rated_leave + 1)

	def test_no_pro_rated_leaves_allocated_before_effective_date(self):
		start_date = get_first_day(add_months(getdate(), -1))
		doj = add_days(start_date, 5)

		self.employee.date_of_joining = doj
		self.employee.save()

		# assigning before DOJ
		frappe.flags.current_date = add_days(doj, -1)
		leave_policy_assignments = make_policy_assignment(
			self.employee, allocate_on_day="Date of Joining", start_date=start_date
		)
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		self.assertEqual(leaves_allocated, 0.0)

	def test_pro_rated_allocation_via_scheduler(self):
		start_date = get_first_day(add_months(getdate(), -1))
		doj = add_days(start_date, 5)

		self.employee.date_of_joining = doj
		self.employee.save()

		# assigning before DOJ, no leaves allocated initially
		frappe.flags.current_date = add_days(doj, -1)
		leave_policy_assignments = make_policy_assignment(
			self.employee, allocate_on_day="First Day", start_date=start_date
		)

		# pro-rated leaves allocated during the first month
		frappe.flags.current_date = add_days(doj, -1)
		allocate_earned_leaves()
		leaves_allocated = get_allocated_leaves(leave_policy_assignments[0])
		pro_rated_leave = calculate_pro_rated_leaves(1, doj, start_date, get_last_day(start_date))
		self.assertEqual(leaves_allocated, pro_rated_leave)

	@set_holiday_list("Salary Slip Test Holiday List", "_Test Company")
	def test_get_earned_leave_details_for_dashboard(self):
		frappe.flags.current_date = get_year_start(getdate())
		leave_policy_assignments = make_policy_assignment(
			self.employee,
			annual_allocation=6,
			allocate_on_day="First Day",
			start_date=frappe.flags.current_date,
		)
		allocation = frappe.db.get_value(
			"Leave Allocation",
			{"leave_policy_assignment": leave_policy_assignments[0]},
			"name",
		)
		allocation = frappe.get_doc("Leave Allocation", allocation)
		allocation.new_leaves_allocated = 2
		allocation.save()

		allocate_earned_leaves_for_months(6)

		first_sunday = get_first_sunday(self.holiday_list)
		make_leave_application(
			self.employee.name, add_days(first_sunday, 1), add_days(first_sunday, 1), self.leave_type
		)

		# 2 leaves were allocated when the allocation was created
		details = get_leave_details(self.employee.name, allocation.from_date)
		leave_allocation = details["leave_allocation"][self.leave_type]
		expected = {
			"total_leaves": 2.0,
			"expired_leaves": 0.0,
			"leaves_taken": 1.0,
			"leaves_pending_approval": 0.0,
			"remaining_leaves": 1.0,
		}
		self.assertEqual(leave_allocation, expected)

		# total leaves allocated = 5 on the current date
		details = get_leave_details(self.employee.name, frappe.flags.current_date)
		leave_allocation = details["leave_allocation"][self.leave_type]
		expected = {
			"total_leaves": 5.0,
			"expired_leaves": 0.0,
			"leaves_taken": 1.0,
			"leaves_pending_approval": 0.0,
			"remaining_leaves": 4.0,
		}
		self.assertEqual(leave_allocation, expected)

	def tearDown(self):
		frappe.db.set_value("Employee", self.employee.name, "date_of_joining", self.original_doj)
		frappe.db.set_value("Leave Type", self.leave_type, "max_leaves_allowed", 0)
		frappe.flags.current_date = None


def create_earned_leave_type(leave_type, allocate_on_day="Last Day"):
	frappe.delete_doc_if_exists("Leave Type", leave_type, force=1)
	frappe.delete_doc_if_exists("Leave Type", "Test Earned Leave Type", force=1)
	frappe.delete_doc_if_exists("Leave Type", "Test Earned Leave Type 2", force=1)

	return frappe.get_doc(
		dict(
			leave_type_name=leave_type,
			doctype="Leave Type",
			is_earned_leave=1,
			earned_leave_frequency="Monthly",
			rounding=0.5,
			is_carry_forward=1,
			allocate_on_day=allocate_on_day,
			max_leaves_allowed=0,
		)
	).insert()


def create_leave_period(name, start_date=None):
	frappe.delete_doc_if_exists("Leave Period", name, force=1)

	if not start_date:
		start_date = get_first_day(getdate())

	return frappe.get_doc(
		dict(
			name=name,
			doctype="Leave Period",
			from_date=start_date,
			to_date=add_months(start_date, 12),
			company="_Test Company",
			is_active=1,
		)
	).insert()


def make_policy_assignment(
	employee,
	allocate_on_day="Last Day",
	earned_leave_frequency="Monthly",
	start_date=None,
	annual_allocation=12,
	carry_forward=0,
	assignment_based_on="Leave Period",
):
	leave_type = create_earned_leave_type("Test Earned Leave", allocate_on_day)
	leave_period = create_leave_period("Test Earned Leave Period", start_date=start_date)
	leave_policy = frappe.get_doc(
		{
			"doctype": "Leave Policy",
			"title": "Test Earned Leave Policy",
			"leave_policy_details": [
				{"leave_type": leave_type.name, "annual_allocation": annual_allocation}
			],
		}
	).insert()

	data = {
		"assignment_based_on": assignment_based_on,
		"leave_policy": leave_policy.name,
		"leave_period": leave_period.name,
		"carry_forward": carry_forward,
	}

	leave_policy_assignments = create_assignment_for_multiple_employees(
		[employee.name], frappe._dict(data)
	)
	return leave_policy_assignments


def get_allocated_leaves(assignment):
	return frappe.db.get_value(
		"Leave Allocation",
		{"leave_policy_assignment": assignment},
		"total_leaves_allocated",
	)


def allocate_earned_leaves_for_months(months):
	for i in range(0, months):
		frappe.flags.current_date = add_months(frappe.flags.current_date, 1)
		allocate_earned_leaves()
