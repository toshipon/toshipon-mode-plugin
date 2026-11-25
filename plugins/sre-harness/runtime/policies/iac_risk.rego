# Risk classification sample for a Terraform-style plan (`terraform show -json`).
#
# This is a sample, not a complete policy. It reads `input.resource_changes[].change` and
# gives each change a level (none < low < medium < high) and the reasons behind it. The
# state-transition contract of IaC delivery is not checked here; Python does that.
#
# The harness runs it with a bundled capabilities file (iac_risk.capabilities.json) that
# lists only the built-ins this file calls, so a function that could reach the network
# (http.send, net.lookup_ip_addr) or read the environment (opa.runtime) does not compile.
# Calling a new built-in here means adding it to that file; see the README.
#
#   opa eval --format json --strict --strict-builtin-errors \
#     --capabilities iac_risk.capabilities.json --data iac_risk.rego \
#     --stdin-input data.sre_harness.iac_risk.result < plan.json
package sre_harness.iac_risk

# Resource types whose every change is high: identity, network boundaries, public access.
sensitive_types := {
	"aws_iam_policy",
	"aws_iam_role",
	"aws_iam_role_policy",
	"aws_iam_role_policy_attachment",
	"aws_iam_user_policy",
	"aws_s3_bucket_acl",
	"aws_s3_bucket_policy",
	"aws_s3_bucket_public_access_block",
	"aws_security_group",
	"aws_security_group_rule",
	"aws_vpc_security_group_egress_rule",
	"aws_vpc_security_group_ingress_rule",
}

# Attributes that make a resource reachable from outside when they turn true.
public_flags := {"associate_public_ip_address", "map_public_ip_on_launch", "publicly_accessible"}

tag_keys := {"tags", "tags_all"}

known_actions := {"no-op", "read", "create", "update", "delete"}

reason_level := {
	"delete": "high",
	"replace": "high",
	"sensitive_type": "high",
	"public_setting": "high",
	"unknown_action": "medium",
	"create": "medium",
	"update": "medium",
	"tags_only": "low",
}

names := ["none", "low", "medium", "high"]

rank := {"none": 0, "low": 1, "medium": 2, "high": 3}

obj(x) := x if is_object(x)

else := {}

changes_anything(change) if {
	some action in change.actions
	not action in {"no-op", "read"}
}

reasons[i] contains "delete" if {
	input.resource_changes[i].change.actions == ["delete"]
}

reasons[i] contains "replace" if {
	actions := input.resource_changes[i].change.actions
	"delete" in actions
	"create" in actions
}

reasons[i] contains "create" if {
	input.resource_changes[i].change.actions == ["create"]
}

reasons[i] contains "tags_only" if {
	change := input.resource_changes[i].change
	change.actions == ["update"]
	object.remove(obj(change.before), tag_keys) == object.remove(obj(change.after), tag_keys)
}

reasons[i] contains "update" if {
	change := input.resource_changes[i].change
	change.actions == ["update"]
	object.remove(obj(change.before), tag_keys) != object.remove(obj(change.after), tag_keys)
}

reasons[i] contains "sensitive_type" if {
	resource := input.resource_changes[i]
	resource.type in sensitive_types
	changes_anything(resource.change)
}

reasons[i] contains "public_setting" if {
	change := input.resource_changes[i].change
	some flag in public_flags
	obj(change.after)[flag] == true
	not obj(change.before)[flag] == true
}

reasons[i] contains "public_setting" if {
	change := input.resource_changes[i].change
	is_string(obj(change.after).acl)
	startswith(obj(change.after).acl, "public-")
	not obj(change.before).acl == obj(change.after).acl
}

reasons[i] contains "unknown_action" if {
	some action in input.resource_changes[i].change.actions
	not action in known_actions
}

level_of(i) := names[max({0} | {rank[reason_level[r]] | some r in reasons[i]})]

changes := [item |
	some i, resource in input.resource_changes
	item := {
		"address": resource.address,
		"type": resource.type,
		"actions": resource.change.actions,
		"level": level_of(i),
		"reasons": sort([r | some r in reasons[i]]),
	}
]

result := {
	"level": names[max({0} | {rank[item.level] | some item in changes})],
	"changes": changes,
}
